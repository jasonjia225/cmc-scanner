"""Public-data CMC proxy scanner. Research only, never places trades.
No data = no signal. Never equates public reference prices with CMC CFD quotes.
"""
import json, os, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import quote
import numpy as np
import pandas as pd
import requests

S = requests.Session()
S.headers.update({'User-Agent':'Mozilla/5.0 CMC-research-scanner/1.0','Accept':'application/json'})
UTC=timezone.utc
NOW=datetime.now(UTC)
# V5: fifteen instruments; VIX is context only.
ASSETS = [
 ('NVDA','NVDA','Yahoo Finance US equity'),
 ('MU','MU','Yahoo Finance US equity'),
 ('MSTR','MSTR','Yahoo Finance US equity'),
 ('TSLA','TSLA','Yahoo Finance US equity'),
 ('SNDK','SNDK','Yahoo Finance US equity'),
 ('Germany 40 - Cash','^GDAXI','Yahoo Finance DAX cash index proxy'),
 ('US NDAQ 100 - Cash','^NDX','Yahoo Finance NDX cash index proxy'),
 ('Bitcoin (USD)',None,'Coinbase BTC-USD spot'),
 ('Ethereum (USD)',None,'Coinbase ETH-USD spot'),
 ('Hong Kong 50 - Cash','^HSI','Yahoo Finance Hang Seng cash index proxy'),
 ('USD/JPY','JPY=X','Yahoo Finance FX indicative'),
 ('NZD/USD','NZDUSD=X','Yahoo Finance FX indicative'),
 ('Gold - Cash','GC=F','Yahoo Finance COMEX gold futures proxy'),
 ('Silver - Cash','SI=F','Yahoo Finance COMEX silver futures proxy'),
 ('Crude Oil West Texas - Cash','CL=F','Yahoo Finance NYMEX WTI futures proxy'),
]
# Actual data providers vary. This scan does not claim guaranteed exchange real-time data.
TF={'15m':900,'1h':3600,'1d':86400}

def request_json(url,params=None):
    try:
        r=S.get(url,params=params,timeout=13)
        if r.status_code!=200: return None,f'HTTP {r.status_code}'
        return r.json(),None
    except (requests.RequestException,ValueError) as e:
        return None,f'{type(e).__name__}: {str(e)[:120]}'

def clean(df,seconds,now=NOW):
    if df is None or df.empty: return None
    df=df.copy()
    df.index=pd.to_datetime(df.index,utc=True)
    df=df[~df.index.duplicated(keep='last')].sort_index()
    for k in ['open','high','low','close']:
        df[k]=pd.to_numeric(df[k],errors='coerce')
    df=df.dropna(subset=['open','high','low','close'])
    # Timestamps are candle OPEN times. Exclude unfinished bars.
    cutoff=pd.Timestamp(now)
    return df[df.index+pd.Timedelta(seconds=seconds)<=cutoff]

def yahoo(symbol,interval,range_):
    errors=[]
    for host in ['query1.finance.yahoo.com','query2.finance.yahoo.com']:
        url=f'https://{host}/v8/finance/chart/{quote(symbol,safe="")}'
        obj,err=request_json(url,{'interval':interval,'range':range_,'includePrePost':'false'})
        if err: errors.append(f'{host}: {err}');continue
        try:
            result=obj['chart']['result'][0]
            ts=result['timestamp']; q=result['indicators']['quote'][0]
            df=pd.DataFrame({**{k:q[k] for k in ['open','high','low','close']},'volume':q.get('volume',[None]*len(ts))},index=pd.to_datetime(ts,unit='s',utc=True))
            return clean(df,TF[interval]),None
        except (TypeError,KeyError,IndexError) as e: errors.append(f'{host}: malformed response {e}')
    return None,'; '.join(errors)

def coinbase(symbol,seconds):
    # Coinbase Exchange candles: [time,low,high,open,close,volume], max 300 buckets.
    # Chunking 200 candles per request, enough for EMA50 and daily trends.
    rows=[]; errors=[]
    end=int(NOW.timestamp()//seconds*seconds)
    for batch in range(3):
        start=end-200*seconds
        obj,err=request_json(f'https://api.exchange.coinbase.com/products/{symbol}/candles',
            {'granularity':seconds,'start':datetime.fromtimestamp(start,UTC).isoformat(),
             'end':datetime.fromtimestamp(end,UTC).isoformat()})
        if err: errors.append(err);break
        if not isinstance(obj,list): errors.append('unexpected JSON response');break
        rows.extend(obj);end=start
        time.sleep(0.18)
    if not rows:return None,'Coinbase: '+('; '.join(errors) or 'empty candles')
    try:
        df=pd.DataFrame(rows,columns=['time','low','high','open','close','volume'])
        df.index=pd.to_datetime(df.pop('time'),unit='s',utc=True)
        return clean(df,seconds),('; '.join(errors) if errors else None)
    except (ValueError,KeyError) as e:return None,f'Coinbase parse: {e}'

def aggregate_4h(hourly, name=None):
    if hourly is None or hourly.empty:return None
    # UTC 4h buckets; accept only four strictly consecutive hourly candles.
    groups=[]
    if name in {'NVDA','MU','MSTR','TSLA','SNDK','US NDAQ 100 - Cash'}:
        local=hourly.index.tz_convert('America/New_York')
        # Only full 09:30–13:30 regular-session blocks; discard partial session tail.
        mask=(local.hour*60+local.minute>=570)&(local.hour*60+local.minute<810)
        hourly=hourly.loc[mask]
        buckets=hourly.index.tz_convert('America/New_York').normalize()+pd.Timedelta(hours=9,minutes=30)
        buckets=buckets.tz_convert('UTC')
    else:
        buckets=hourly.index.floor('4h')
    for t,g in hourly.groupby(buckets):
        expected=pd.date_range(t,periods=4,freq='h',tz='UTC')
        if len(g)==4 and g.index.equals(expected):
            groups.append((t,g.open.iloc[0],g.high.max(),g.low.min(),g.close.iloc[-1],g.volume.sum(min_count=4) if 'volume' in g else np.nan))
    if not groups:return None
    return pd.DataFrame(groups,columns=['time','open','high','low','close','volume']).set_index('time')

def metrics(df):
    if df is None or len(df)<60:return None
    c=df.close; h=df.high; l=df.low
    ema20=c.ewm(span=20,adjust=False).mean();ema50=c.ewm(span=50,adjust=False).mean()
    d=c.diff(); gain=d.clip(lower=0).ewm(alpha=1/14,adjust=False,min_periods=14).mean()
    loss=(-d.clip(upper=0)).ewm(alpha=1/14,adjust=False,min_periods=14).mean()
    rs=gain/loss.replace(0,np.nan);rsi=100-100/(1+rs);rsi=rsi.mask((loss==0)&(gain>0),100).mask((loss==0)&(gain==0),50)
    tr=pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    atr=tr.ewm(alpha=1/14,adjust=False,min_periods=14).mean()
    return {'close':float(c.iloc[-1]),'ema20':float(ema20.iloc[-1]),
            'ema50':float(ema50.iloc[-1]),'rsi14':float(rsi.iloc[-1]) if pd.notna(rsi.iloc[-1]) else None,
            'atr14':float(atr.iloc[-1]),'support20':float(l.iloc[-21:-1].min()),
            'resistance20':float(h.iloc[-21:-1].max()),'bars':len(df)}

def iso(dt):return dt.isoformat().replace('+00:00','Z')

def boll_events(name,bars,now=NOW):
    candidates=[]; alerts=[]
    seconds={**TF,'4h':14400}
    for t,d in bars.items():
        if len(d)<28:continue
        end=d.index[-1].to_pydatetime()+timedelta(seconds=seconds[t])
        # Only report recent closed bars, not historical weekend repeats.
        if not 0 <= (now-end).total_seconds() < seconds[t]+900:continue
        c=d.close; mid=c.rolling(26).mean(); sd=c.rolling(26).std(ddof=0)
        tr=pd.concat([d.high-d.low,(d.high-c.shift()).abs(),(d.low-c.shift()).abs()],axis=1).max(axis=1)
        atr=float(tr.ewm(alpha=1/14,adjust=False,min_periods=14).mean().iloc[-1])
        if not np.isfinite(atr) or atr<=0:continue
        delta=c.diff(); g=delta.clip(lower=0).ewm(alpha=1/14,adjust=False,min_periods=14).mean(); loss=(-delta.clip(upper=0)).ewm(alpha=1/14,adjust=False,min_periods=14).mean()
        rsi=(100-100/(1+g/loss.replace(0,np.nan))).mask((loss==0)&(g>0),100).mask((g==0)&(loss>0),0)
        v=d.get('volume',pd.Series(np.nan,index=d.index)); ratio=v/v.shift().rolling(20,min_periods=20).mean().replace(0,np.nan)
        if name in ('US NDAQ 100 - Cash','Hong Kong 50 - Cash','Germany 40 - Cash','USD/JPY','NZD/USD'):ratio=ratio*np.nan
        for sign in (1,-1):
            band=mid-sign*2*sd; outside=d.high<band if sign==1 else d.low>band
            direction='Buy' if sign==1 else 'Sell'; entry=float(c.iloc[-1]); stamp=iso(end)
            if bool(outside.iloc[-1]) and not bool(outside.iloc[-2]):
                alerts.append(dict(name=name,timeframe=t,direction=direction,reference_price=entry,reference_candle_close_utc=stamp,score=5,reason='第一根整根出轨；等待下一根收盘反转'))
            if not bool(outside.iloc[-2]) or bool(outside.iloc[-3]):continue
            # Do not bridge missing/inactive sessions as if adjacent intraday bars.
            if t!='1d' and (d.index[-1]-d.index[-2]).total_seconds()!=seconds[t]:continue
            o=float(d.open.iloc[-1]); h=float(d.high.iloc[-1]); l=float(d.low.iloc[-1]); body=abs(entry-o)
            wick=min(o,entry)-l if sign==1 else h-max(o,entry)
            spike=bool(ratio.iloc[-1]>=2 or ratio.iloc[-2]>=2)
            back=sign*(entry-float(band.iloc[-1]))>=0
            reversal=sign*(entry-o)>0 and (wick>=max(body,.1*atr) or spike or back or sign*(entry-float(c.iloc[-2]))>0)
            # Reject substantial new adverse extremes even on a positive candle.
            adverse=float(d.low.iloc[-2])-l if sign==1 else h-float(d.high.iloc[-2])
            if not reversal or adverse>.5*atr:continue
            extreme=bool((rsi.iloc[-2:]<30).any() if sign==1 else (rsi.iloc[-2:]>70).any())
            score=8+int(spike)+int(extreme)
            stop=float(d.low.iloc[-2:].min()-.3*atr if sign==1 else d.high.iloc[-2:].max()+.3*atr)
            risk=sign*(entry-stop); tp1=float(mid.iloc[-1]); tp2=float((mid+sign*2*sd).iloc[-1]);
            if risk<=0 or min(entry,stop,tp1,tp2)<=0:continue
            rr1=sign*(tp1-entry)/risk; rr2=sign*(tp2-entry)/risk
            chase=abs(entry-float(c.iloc[-2]))/atr
            obstacle=float(d.high.iloc[-21:-2].max() if sign==1 else d.low.iloc[-21:-2].min())
            blocked=0<sign*(obstacle-entry)<2*risk
            ready=rr1>=2 and rr2>=rr1 and chase<=1 and risk<=4*atr and not blocked
            candidates.append(dict(mode='BOLL Reversal',timeframe=t,direction=direction,score=float(score),reason=f'整根出轨5＋第二根反转3＋放量{int(spike)}＋RSI极端{int(extreme)}',confirmation='第二根已收盘；核对CMC及成本',reference_candle_close_utc=stamp,reference_price=entry,proxy_only=True,setup_ready=bool(ready),risk_note=f'距离首根收盘 {chase:.2f} ATR；近端阻力/支撑挡住2R={blocked}；中轨2R达标={rr1>=2}；轨道为当前快照，会移动',reference_entry=entry,reference_stop=stop,reference_tp1=tp1,reference_tp2=tp2,rr_tp1=float(rr1),rr_tp2=float(rr2),stop_pct=100*risk/entry))
    return candidates,alerts

def evaluate(name,bars):
    candidates=[]
    seconds={**TF,'4h':14400}
    fresh={t:d for t,d in bars.items() if len(d)>=60 and 0 <= (NOW-d.index[-1].to_pydatetime()-timedelta(seconds=seconds[t])).total_seconds() <= {'15m':2700,'1h':10800,'4h':43200,'1d':345600}[t]}
    m={t:metrics(d) for t,d in fresh.items()}
    # Volume is not reliable on cash indices or indicative FX.
    volume_ok=name not in ('US NDAQ 100 - Cash','Hong Kong 50 - Cash','Germany 40 - Cash','USD/JPY','NZD/USD')
    def add(mode,t,sign,score,stop,reason,ready,anchor):
        if score<8:return
        a=m[t]; entry=a['close']; atr=a['atr14']; risk=sign*(entry-stop)
        if not np.isfinite(risk) or risk<=0 or entry<=0:return
        # Prefer actual known resistance/support before theoretical R targets.
        chase=abs(entry-anchor)/atr
        target=entry+sign*2*risk
        obstacle=a['resistance20'] if sign==1 else a['support20']
        blocked=0<sign*(obstacle-entry)<2*risk
        ready=bool(ready and chase<=1 and risk<=4*atr and not blocked and min(stop,entry+sign*3*risk)>0)
        if min(stop,entry+sign*3*risk)<=0:return
        candidates.append(dict(mode=mode,timeframe=t,direction='Buy' if sign==1 else 'Sell',score=float(score),reason=reason,confirmation='核对CMC报价、点差、交易时段及事件；结构目标须验证',reference_candle_close_utc=iso(fresh[t].index[-1].to_pydatetime()+timedelta(seconds=seconds[t])),reference_price=entry,proxy_only=True,setup_ready=ready,risk_note=f'距离参考触发点 {chase:.2f} ATR；近端阻力/支撑挡住2R={blocked}；目标为理论值',reference_entry=entry,reference_stop=float(stop),reference_tp1=float(target),reference_tp2=float(entry+sign*3*risk),rr_tp1=2.,rr_tp2=3.,stop_pct=100*risk/entry))
    for t,d in fresh.items():
        if t=='1d':continue
        a=m[t]; atr=a['atr14']; entry=a['close']
        if not np.isfinite(atr) or atr<=0:continue
        c=d.close; mid=c.rolling(26).mean(); sd=c.rolling(26).std(ddof=0)
        v=d.get('volume',pd.Series(np.nan,index=d.index)); ratio=v/v.shift().rolling(20,min_periods=20).mean().replace(0,np.nan) if volume_ok else v*np.nan
        for sign in (1,-1):
            aligned=lambda x: sign*(x['ema20']-x['ema50'])>0 and sign*(x['close']-x['ema20'])>0
            other=m.get('1h' if t=='15m' else '4h' if t=='1h' else '1d')
            momentum=a['rsi14'] is not None and (52<=a['rsi14']<=72 if sign==1 else 28<=a['rsi14']<=48)
            level=a['resistance20'] if sign==1 else a['support20']; breakout=sign*(entry-level)>0
            stop=a['support20']-.3*atr if sign==1 else a['resistance20']+.3*atr
            score=4*aligned(a)+2*bool(other and aligned(other))+2*breakout+2*momentum
            if aligned(a):add('Trend',t,sign,score,stop,'趋势、突破及动量独立评分',breakout,level)
            width=a['resistance20']-a['support20']; vol=bool(ratio.iloc[-1]>=2)
            score=4*breakout+2*(width<=5*atr)+2*vol+2*momentum
            if breakout:add('Range Breakout',t,sign,score,level-sign*atr,'横盘突破、量能及动量',True,level)
            # Confirmed local pivots: two bars on both sides, no future bars.
            x=(d.low if sign==1 else -d.high).iloc[-60:].to_numpy(); n=len(x)
            piv=[i for i in range(2,n-2) if x[i]<=min(x[i-2:i]) and x[i]<=min(x[i+1:i+3])]
            for j in reversed(piv):
                if n-1-j>8:break
                found=False
                for i in reversed([k for k in piv if 4<=j-k<=40]):
                    if abs(x[i]-x[j])>abs(x[i])*.01 or x[j]<x[i]-.5*atr:continue
                    neck=(d.high.iloc[-60+i:-60+j].max() if sign==1 else d.low.iloc[-60+i:-60+j].min())
                    if sign*neck-max(x[i],x[j])<atr:continue
                    if min(x[j+1:])<min(x[i],x[j]):continue
                    vr=ratio.iloc[-60:].iloc[max(0,j-2):min(n,j+3)]
                    spike=bool((vr>=2).any())
                    if not spike:continue
                    # A volume spike alone is not reversal: require recovery from second pivot.
                    if sign*entry-x[j]<.3*atr:continue
                    div=False
                    delta=c.diff();g=delta.clip(lower=0).ewm(alpha=1/14,adjust=False).mean();loss=(-delta.clip(upper=0)).ewm(alpha=1/14,adjust=False).mean();r=100-100/(1+g/loss.replace(0,np.nan));r=r.iloc[-60:]
                    div=bool(sign*(r.iloc[j]-r.iloc[i])>0 and x[j]<=x[i])
                    crossed=sign*(entry-neck)>0
                    hourly=m.get('1h'); turn=bool(hourly and sign*(hourly['close']-hourly['ema20'])>0)
                    score=8+int(crossed)+.5*div+.5*turn
                    add('Double Bottom/Top Reversal',t,sign,score,sign*(min(x[i],x[j])-.3*atr),f'双底/顶4分＋放量4分（{vr.max():.2f}倍）；颈线 {neck:.6g}；背离={div}',True,sign*x[j]+sign*.3*atr)
                    found=True;break
                if found:break
    boll,alerts=boll_events(name,bars)
    candidates.extend(boll)
    candidates.sort(key=lambda w:(w['score'],w['setup_ready'],-w['stop_pct']),reverse=True)
    return candidates,None if candidates else '无8分候选或数据不足/过期',alerts

def scan():
    output={'scan_utc':iso(NOW),'timezone':'UTC','type':'public_proxy_research_only',
            'version':'V5.3','cmc_quotes_verified':False,'high_quality_signals':[], 'assets':[], 'watch_candidates':[], 'research_setups':[], 'boll_alerts':[]}
    vix,err=yahoo('^VIX','15m','5d')
    output['risk_context']={'VIX':metrics(vix),'error':err} if vix is not None else {'VIX':None,'error':err}
    for name,symbol,source in ASSETS:
        item={'name':name,'proxy_source':source,'currency':'USD' if 'Bitcoin' in name or 'Ethereum' in name else None,
              'timeframes':{},'candle_close_utc':{},'last_complete_candle_utc':None,'errors':{},'metrics':{},'watch':None}
        bars={}
        if name.startswith('China A50'):
            for tf in ['15m','1h','4h','1d']:item['timeframes'][tf]=False
            item['errors']['contract']='CMC specific active month and expiry not independently verified; signal disabled'
        else:
            for tf in ['15m','1h','1d']:
                if symbol:
                    df,err=yahoo(symbol,tf,{'15m':'5d','1h':'3mo','1d':'3mo'}[tf])
                else:
                    df,err=coinbase('BTC-USD' if 'Bitcoin' in name else 'ETH-USD',TF[tf])
                if df is not None and len(df)>0:
                    bars[tf]=df;item['timeframes'][tf]=True
                    item['metrics'][tf]=metrics(df)
                    closed=iso(df.index[-1].to_pydatetime()+timedelta(seconds=TF[tf]))
                    item['candle_close_utc'][tf]=closed
                    if not item['last_complete_candle_utc'] or closed>item['last_complete_candle_utc']:
                        item['last_complete_candle_utc']=closed
                else:item['timeframes'][tf]=False
                if err:item['errors'][tf]=err
            four=aggregate_4h(bars.get('1h'),name)
            item['timeframes']['4h']=four is not None and not four.empty
            if four is not None and not four.empty:
                bars['4h']=four
                item['metrics']['4h']=metrics(four)
                closed=iso(four.index[-1].to_pydatetime()+timedelta(hours=4))
                item['candle_close_utc']['4h']=closed
                if not item['last_complete_candle_utc'] or closed>item['last_complete_candle_utc']:
                    item['last_complete_candle_utc']=closed
            else:item['errors']['4h']='insufficient contiguous 1h candles for complete UTC-aligned 4h bar'
            watches,item['screen_reason'],alerts=evaluate(name,bars)
            output['boll_alerts'].extend(alerts)
            item['watch']=watches[0] if watches else None
            for w in watches:
                candidate={'name':name,'proxy_source':source,**w}
                output['watch_candidates'].append(candidate)
                if candidate['setup_ready']:output['research_setups'].append(candidate)
        output['assets'].append(item)
    output['watch_candidates'].sort(key=lambda x:x['score'], reverse=True)
    output['research_setups'].sort(key=lambda x:x['score'], reverse=True)
    # Never mark executable high-quality signals without verified CMC spread/events.
    return output

def nz(s):
    if not s:return '无'
    return datetime.fromisoformat(s.replace('Z','+00:00')).astimezone(ZoneInfo('Pacific/Auckland')).strftime('%m-%d %H:%M')

SHORT={x[0]:x[0] for x in ASSETS}
SOURCES=[x[2] for x in ASSETS]

def report(data, previous=None):
    previous=previous or {}
    lines=['CMC V5.3 | '+nz(data['scan_utc'])+' 新西兰时间','本轮暂无已核实的出手机会。']
    seen=set(); shown=0
    watches=sorted(data['watch_candidates'],key=lambda w:(w['setup_ready'],w['score']),reverse=True)
    for w in watches:
        key=(w['name'],w['mode'],w['direction'],w['timeframe']) if w['mode']=='BOLL Reversal' else (w['name'],'other',w['direction'])
        if w['score']<8 or key in seen:continue
        seen.add(key);shown+=1
        lines.append(f"{w['name']} | {w['direction']} | {w['mode']} | {w['timeframe']} | {w['score']:.1f}/10")
        if w['setup_ready']:
            lines += [w['reason'],f"技术入场参考 {w['reference_entry']:.8g}（{nz(w['reference_candle_close_utc'])}）",'来源：'+w['proxy_source'],f"止损 {w['reference_stop']:.8g} | TP1 {w['reference_tp1']:.8g} | TP2 {w['reference_tp2']:.8g}",f"盈亏比 {w['rr_tp1']:.2f}:1，未计成本；CMC及事件风险待核实"]
        else:
            note=w.get('risk_note','')
            import re
            match=re.search(r'([0-9.]+) ATR',note)
            reasons=[]
            if match and float(match.group(1))>1:reasons.append('距离触发点过远，勿追单')
            if '挡住2R=True' in note:reasons.append('支撑/阻力限制目标')
            if w['rr_tp1']<2:reasons.append('中轨盈亏比不足2:1')
            lines.append('仅观察：'+('；'.join(reasons) or '入场条件或风险参数未达标'))
    if not shown:lines.append('暂无8分以上技术候选。')
    oldkeys=set(previous.get('alert_keys',[]))
    current=[]
    for w in data.get('boll_alerts',[]):
        k='|'.join(str(w[x]) for x in ('name','direction','timeframe','reference_candle_close_utc'))
        current.append(k)
        if k not in oldkeys:lines.append(f"⚠️ BOLL预警 | {w['name']} | {w['direction']} | {w['timeframe']} | 等待第二根收盘反转")
    ctx=data.get('risk_context',{}).get('VIX')
    lines.append('VIX风险参考：'+(f"{ctx['close']:.2f} | "+('极高风险' if ctx['close']>=45 else '高风险' if ctx['close']>=35 else '偏高' if ctx['close']>=25 else '常规' if ctx['close']>=15 else '低波动') if ctx else '未取得'))
    groups={}
    for a in data['assets']:
        missing=tuple(t for t in ('15m','1h','4h','1d') if not a['timeframes'].get(t))
        errors=tuple(sorted(a.get('errors',{})))
        if missing or errors:
            key='缺少'+ '/'.join(missing) if missing else '接口部分异常'
            groups.setdefault(key,[]).append(a['name'])
    issue_keys=sorted(k+':'+','.join(sorted(v)) for k,v in groups.items())
    if issue_keys!=previous.get('issue_keys',[]):
        if groups:
            for k,names in groups.items():lines.append(f"⚠️ 数据：{len(names)}个标的{k}（{'、'.join(names)}）；其他可用周期继续分析。")
        else:lines.append('数据异常已恢复。')
    data['notification_state']={'alert_keys':sorted(oldkeys.union(current))[-2000:],'issue_keys':issue_keys}
    lines.append('评分非盈利概率；下单前核对CMC实时价格、点差及事件风险。')
    return '\n'.join(lines)

if __name__=='__main__':
    folder=Path('reports');folder.mkdir(exist_ok=True)
    previous={}
    try:previous=json.loads((folder/'latest.json').read_text(encoding='utf-8')).get('notification_state',{})
    except (OSError,ValueError,AttributeError):pass
    result=scan(); message=report(result,previous)
    (folder/'latest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    (folder/'latest.md').write_text(message,encoding='utf-8')
    print(message)
