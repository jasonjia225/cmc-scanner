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
INDEX_FUTURES={'Germany 40 - Cash':'FDAX=F','US NDAQ 100 - Cash':'NQ=F','Hong Kong 50 - Cash':'HSI=F'}

INDEX_EXTRA_FUTURES={'US NDAQ 100 - Cash':['MNQ=F'],'Germany 40 - Cash':['FDXM=F'],'Hong Kong 50 - Cash':[]}
OANDA_INDEX={'US NDAQ 100 - Cash':'NAS100_USD','Germany 40 - Cash':'DE30_EUR','Hong Kong 50 - Cash':'HK33_HKD'}

INDEX_ETFS={'Germany 40 - Cash':['EXS1.DE'], 'US NDAQ 100 - Cash':['QQQ'], 'Hong Kong 50 - Cash':['2800.HK']}

def volume_allowed(name,d):
    return name not in ('USD/JPY','NZD/USD') and (name not in INDEX_FUTURES or (d.attrs.get('futures',False) or d.attrs.get('etf',False)))

def session_status(name,source,df,tf):
    if df is None or df.empty:return '获取失败或无数据'
    age=(NOW-df.index[-1].to_pydatetime()).total_seconds()-({'15m':900,'1h':3600,'4h':14400,'1d':86400}[tf])
    if age<= {'15m':2700,'1h':10800,'4h':43200,'1d':345600}[tf]:return '可用'
    if name in INDEX_FUTURES or name in ('NVDA','MU','MSTR','TSLA','SNDK'):
        zone='Asia/Hong_Kong' if name.startswith('Hong') else 'Europe/Berlin' if name.startswith('Germany') else 'America/New_York'
        local=NOW.astimezone(ZoneInfo(zone))
        if local.weekday()==5 or (local.weekday()==6 and ('futures' not in source or local.hour<18)):
            return '周末通常休市（交易所日历未核实）'
    return '数据延迟或过期（休市状态未核实）'

def resolve_conflicts(watches,details):
    conflicts=[]
    for tf in {a['timeframe'] for a in details} | {w['timeframe'] for w in watches}:
        ds=[a for a in details if a['timeframe']==tf]
        ws=[w for w in watches if w['timeframe']==tf]
        sides={a['direction'] for a in ds+ws}
        if len(sides)<2:continue
        rank={'疑似第二顶底':1,'结构确认':2,'颈线已突破':3}
        scores={side:max([rank.get(a['stage'],0) for a in ds if a['direction']==side]+[0]) for side in sides}
        buy,sell=scores['Buy'],scores['Sell']
        preferred='Buy' if buy>sell else 'Sell' if sell>buy else None
        for w in ws:w['setup_ready']=False;w['conflict']=True
        conflicts.append(dict(timeframe=tf,preferred=preferred,reason='多空冲突；等待进一步确认',buy_stage=buy,sell_stage=sell, sides={side:[{'score':a.get('score'), 'neckline':a.get('neckline'), 'stage':a.get('stage')} for a in ds if a['direction']==side] for side in sides}))
    return conflicts

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

def oanda_index(name,tf):
    # Optional authorized read-only CFD feed. Tick counts are NOT futures volume.
    token=os.getenv('OANDA_API_TOKEN')
    if not token:return None,'OANDA_API_TOKEN not configured'
    host='api-fxtrade.oanda.com' if os.getenv('OANDA_ENV')=='live' else 'api-fxpractice.oanda.com'
    try:
        r=S.get(f'https://{host}/v3/instruments/{OANDA_INDEX[name]}/candles',
          headers={'Authorization':'Bearer '+token},params={'granularity':{'15m':'M15','1h':'H1','1d':'D'}[tf], 'count':500,'price':'M'},timeout=13)
        if r.status_code!=200:return None,f'OANDA HTTP {r.status_code}'
        rows=[{'time':c['time'],**{k:float(c['mid'][v]) for k,v in zip(('open','high','low','close'),'ohlc')},'volume':np.nan} for c in r.json()['candles'] if c['complete']]
        if not rows:return None,'OANDA no complete candles'
        return clean(pd.DataFrame(rows).set_index('time'),TF[tf]),None
    except (requests.RequestException,ValueError,KeyError,TypeError):return None,'OANDA request or parse failure'

def twelve_index(name,tf):
    key=os.getenv('TWELVE_DATA_API_KEY')
    suffix={'US NDAQ 100 - Cash':'NDX','Germany 40 - Cash':'DAX','Hong Kong 50 - Cash':'HSI'}[name]
    symbol=os.getenv('TWELVE_DATA_'+suffix+'_SYMBOL')
    if not key or not symbol:return None,'Twelve Data key or instrument not configured'
    obj,err=request_json('https://api.twelvedata.com/time_series',{'symbol':symbol,'interval':{'15m':'15min','1h':'1h','1d':'1day'}[tf],'outputsize':1000,'timezone':'UTC','apikey':key})
    if err:return None,'Twelve Data request failed'
    try:
        rows=[{'time':r['datetime'],**{k:float(r[k]) for k in ('open','high','low','close')},'volume':np.nan} for r in obj['values']]
        return clean(pd.DataFrame(rows).set_index('time'),TF[tf]),None
    except (KeyError,TypeError,ValueError):return None,'Twelve Data unavailable or invalid candles'

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
    if not hourly.attrs.get('futures') and name in {'NVDA','MU','MSTR','TSLA','SNDK','US NDAQ 100 - Cash'}:
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
    result=pd.DataFrame(groups,columns=['time','open','high','low','close','volume']).set_index('time');result.attrs.update(hourly.attrs);return result

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
        if not volume_allowed(name,d):ratio=ratio*np.nan
        for sign in (1,-1):
            band=mid-sign*2*sd; outside=d.high<band if sign==1 else d.low>band
            direction='Buy' if sign==1 else 'Sell'; entry=float(c.iloc[-1]); stamp=iso(end)
            if bool(outside.iloc[-1]) and not bool(outside.iloc[-2]):
                alerts.append(dict(name=name,timeframe=t,direction=direction,reference_price=entry,reference_candle_close_utc=stamp,trigger_low=float(d.low.iloc[-1]),trigger_high=float(d.high.iloc[-1]),score=5,reason='第一根整根出轨；等待下一根收盘反转'))
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

def evaluate(name,bars, double_alerts=None):
    if double_alerts is None: double_alerts=[]
    candidates=[]
    seconds={**TF,'4h':14400}
    fresh={t:d for t,d in bars.items() if len(d)>=60 and 0 <= (NOW-d.index[-1].to_pydatetime()-timedelta(seconds=seconds[t])).total_seconds() <= {'15m':2700,'1h':10800,'4h':43200,'1d':345600}[t]}
    m={t:metrics(d) for t,d in fresh.items()}
    # Volume is not reliable on cash indices or indicative FX.
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
        v=d.get('volume',pd.Series(np.nan,index=d.index)); ratio=v/v.shift().rolling(20,min_periods=20).mean().replace(0,np.nan) if volume_allowed(name,d) else v*np.nan
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
            # First pivot: two closed bars on each side. Second: early warning,
            # then two closed bars without a new extreme. No future data.
            z=d.iloc[-60:]; x=(z.low if sign==1 else -z.high).to_numpy(); n=len(x)
            q=ratio.iloc[-60:]; raw=v.iloc[-60:]
            piv=[i for i in range(2,n-2) if x[i]<=min(x[i-2:i]) and x[i]<=min(x[i+1:i+3])]
            delta=c.diff(); g=delta.clip(lower=0).ewm(alpha=1/14,adjust=False).mean(); loss=(-delta.clip(upper=0)).ewm(alpha=1/14,adjust=False).mean()
            r=(100-100/(1+g/loss.replace(0,np.nan))).mask((loss==0)&(g>0),100).iloc[-60:]
            def stamp(k): return iso(z.index[k].to_pydatetime()+timedelta(seconds=seconds[t]))
            found=False
            for j in range(n-1,max(1,n-9),-1):
                if x[j]>min(x[j-2:j]) or (j<n-1 and min(x[j+1:])<x[j]):continue
                for i in reversed([k for k in piv if 8<=j-k<=40]):
                    if abs(x[i]-x[j])>abs(x[i])*.005 or x[j]<x[i]-.5*atr:continue
                    neck=float(z.high.iloc[i+1:j].max() if sign==1 else z.low.iloc[i+1:j].min())
                    if sign*neck-max(x[i],x[j])<2*atr:continue
                    # Real approach into BOTH pivots; no flat retests or weekend bridges.
                    if i<4 or j<4:continue
                    if t!='1d' and any(z.index[k]-z.index[k-1]!=pd.Timedelta(seconds=seconds[t]) for k in range(i-3,n)):continue
                    approach=lambda k: max(sign*float(v) for v in z.close.iloc[k-4:k])-x[k]
                    if min(approach(i),approach(j))<1.5*atr:continue
                    if sign*(float(z.close.iloc[i+2])-x[i]*sign)<.5*atr:continue
                    first=q.iloc[max(0,i-2):i+3]; second=q.iloc[max(0,j-2):min(n,j+3)]
                    if not bool((first>=2).any()):continue
                    first_peak=float(raw.iloc[max(0,i-2):i+3].max())
                    second_peak=float(raw.iloc[max(0,j-2):j+1].max())
                    shrink=bool(second_peak<first_peak)
                    recovery=sign*entry-x[j]>=.75*atr
                    reversal=bool((second>=2).any()) and recovery and sign*(entry-float(z.open.iloc[j]))>0
                    confirmed=n-1-j>=2 and recovery and (shrink or reversal) and sign*(float(z.close.iloc[j+2])-sign*x[j])>=.5*atr
                    crossed=confirmed and sign*(entry-neck)>0
                    break_indices=[k for k in range(j+2,n) if sign*(float(z.close.iloc[k])-neck)>0 and sign*(float(z.close.iloc[k-1])-neck)<=0]
                    break_spike=any(q.iloc[k]>=2 for k in break_indices)
                    div=bool(sign*(r.iloc[j]-r.iloc[i])>0 and x[j]<=x[i]); hourly=m.get('1h'); turn=bool(hourly and sign*(hourly['close']-hourly['ema20'])>0)
                    score=8+int(crossed)+.5*div+.5*turn if confirmed else 4
                    detail=dict(name=name,timeframe=t,direction='Buy' if sign==1 else 'Sell',first_price=float(sign*x[i]),second_price=float(sign*x[j]),first_utc=stamp(i),second_utc=stamp(j),difference_pct=float(100*abs(x[i]-x[j])/abs(x[i])),neckline=neck,first_volume_ratio=float(first.max()),second_volume_ratio=float(second.max()) if pd.notna(second.max()) else None,first_volume_utc=iso(first.idxmax().to_pydatetime()+timedelta(seconds=seconds[t])),breakout_volume_confirmed=bool(break_spike),second_shrinking=shrink,score=score,stage='颈线已突破' if crossed else '结构确认' if confirmed else '疑似第二顶底')
                    double_alerts.append(detail)
                    if confirmed:
                        before=len(candidates)
                        add('Double Bottom/Top Reversal',t,sign,score,sign*(min(x[i],x[j])-.3*atr),f"第一顶底放量{first.max():.2f}倍；颈线 {neck:.6g}；突破放量={break_spike}",crossed,neck)
                        if len(candidates)>before:candidates[-1].update(double_pattern=detail)
                    found=True;break
                if found:break
    boll,alerts=boll_events(name,bars)
    candidates.extend(boll)
    candidates.sort(key=lambda w:(w['score'],w['setup_ready'],-w['stop_pct']),reverse=True)
    return candidates,None if candidates else '无8分候选或数据不足/过期',alerts

def track_boll(name,bars,alerts,watches,previous):
    active={k:dict(v) for k,v in previous.items() if v['name']==name}
    ended=[]
    for w in alerts:
        if w['timeframe'] not in ('4h','1d'):continue
        k='|'.join(str(w[x]) for x in ('name','direction','timeframe','reference_candle_close_utc'))
        if name in INDEX_FUTURES:k+='|'+w.get('proxy_source','unknown')
        active.setdefault(k,{**w,'status':'等待第二根收盘反转'})
    # Recover the immediately preceding trigger when upgrading after confirmation.
    for w in watches:
        t=w['timeframe']
        if w['mode']!='BOLL Reversal' or t not in ('4h','1d'):continue
        d=bars.get(t)
        if d is None or len(d)<2:continue
        stamp=iso(d.index[-2].to_pydatetime()+timedelta(seconds=14400 if t=='4h' else 86400))
        k='|'.join((name,w['direction'],t,stamp))
        if name in INDEX_FUTURES:k+='|'+w.get('proxy_source','unknown')
        a=active.setdefault(k,dict(name=name,direction=w['direction'],timeframe=t,reference_candle_close_utc=stamp,trigger_low=float(d.low.iloc[-2]),trigger_high=float(d.high.iloc[-2])))
        a.update(status='反转已确认',score=w['score'],tp1=w['reference_tp1'],tp2=w['reference_tp2'],confirmed_utc=w['reference_candle_close_utc'])
    for k,a in list(active.items()):
        start=pd.Timestamp(a['reference_candle_close_utc'])
        sign=1 if a['direction']=='Buy' else -1
        edge=a['trigger_low'] if sign==1 else a['trigger_high']
        # Inspect all available closed bars after the trigger, not just latest close.
        chunks=[d.loc[d.index>=start] for d in bars.values() if d is not None]
        broken=any((d.low<edge).any() if sign==1 else (d.high>edge).any() for d in chunks)
        if broken:
            ended.append({**a,'status':'失效：突破触发K线极值'});del active[k];continue
        a['data_unverified']=not any(not d.empty for d in chunks)
        if a.get('confirmed_utc'):
            after=pd.Timestamp(a['confirmed_utc'])
            for target in ('tp1','tp2'):
                if target in a and any(((d.loc[d.index>=after].high>=a[target]).any() if sign==1 else (d.loc[d.index>=after].low<=a[target]).any()) for d in chunks):
                    a[target+'_hit']=True
    return active,ended

def scan(previous=None):
    previous=previous or {}

    output={'scan_utc':iso(NOW),'timezone':'UTC','type':'public_proxy_research_only',
            'version':'V5.8.3','cmc_quotes_verified':False,'high_quality_signals':[], 'assets':[], 'watch_candidates':[], 'research_setups':[], 'boll_alerts':[], 'active_boll':{}, 'ended_boll':[], 'double_alerts':[], 'conflicts':[]}
    vix,err=yahoo('^VIX','15m','5d')
    output['risk_context']={'VIX':metrics(vix),'error':err} if vix is not None else {'VIX':None,'error':err}
    for name,symbol,source in ASSETS:
        item={'name':name,'proxy_source':source,'currency':'USD' if 'Bitcoin' in name or 'Ethereum' in name else None,
              'timeframes':{},'candle_close_utc':{},'last_complete_candle_utc':None,'errors':{},'metrics':{},'watch':None}
        bars={}
        futures_bars={}
        item['source_attempts']={};item['timeframe_sources']={}
        if name in INDEX_FUTURES:
            options=[(INDEX_FUTURES[name],'futures')]+[(x,'futures') for x in INDEX_EXTRA_FUTURES[name]]+[(OANDA_INDEX[name],'cfd'),(os.getenv('TWELVE_DATA_'+{'US NDAQ 100 - Cash':'NDX','Germany 40 - Cash':'DAX','Hong Kong 50 - Cash':'HSI'}[name]+'_SYMBOL') or 'unconfigured','twelve')]+[(x,'etf') for x in INDEX_ETFS[name]]+[(symbol,'cash')]
            for tf in ('15m','1h','1d'):
                fallback=None
                for ticker,kind in options:
                    df,err=(oanda_index(name,tf) if kind=='cfd' else twelve_index(name,tf) if kind=='twelve' else yahoo(ticker,tf,{'15m':'5d','1h':'3mo','1d':'3mo'}[tf]))
                    label=('OANDA ' if kind=='cfd' else 'Twelve Data ' if kind=='twelve' else 'Yahoo Finance ')+ticker+' '+kind+' proxy'
                    status=session_status(name,label,df,tf)
                    valid=df is not None and len(df)>=60
                    vol=valid and df.volume.fillna(0).gt(0).any()
                    item['source_attempts'][tf+' '+ticker]={'error':err,'status':status,'bars':len(df) if df is not None else 0,'volume_available':bool(vol)}
                    if not valid or (kind in ('futures','etf') and not vol):continue
                    df.attrs.update(proxy_source=label,futures=kind=='futures',etf=kind=='etf')
                    if fallback is None:fallback=df
                    if status=='可用':fallback=df;break
                if fallback is not None:futures_bars[tf]=fallback
            source='mixed index proxies';item['proxy_source']=source
        if name.startswith('China A50'):
            for tf in ['15m','1h','4h','1d']:item['timeframes'][tf]=False
            item['errors']['contract']='CMC specific active month and expiry not independently verified; signal disabled'
        else:
            for tf in ['15m','1h','1d']:
                if name in INDEX_FUTURES:
                    df,err=futures_bars.get(tf),None
                elif symbol:
                    df,err=yahoo(symbol,tf,{'15m':'5d','1h':'3mo','1d':'3mo'}[tf])
                else:
                    df,err=coinbase('BTC-USD' if 'Bitcoin' in name else 'ETH-USD',TF[tf])
                if df is not None and len(df)>0:
                    bars[tf]=df;item['timeframes'][tf]=True
                    item['timeframe_sources'][tf]=df.attrs.get('proxy_source',source)
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
                item['timeframe_sources']['4h']=four.attrs.get('proxy_source',source)
                item['metrics']['4h']=metrics(four)
                closed=iso(four.index[-1].to_pydatetime()+timedelta(hours=4))
                item['candle_close_utc']['4h']=closed
                if not item['last_complete_candle_utc'] or closed>item['last_complete_candle_utc']:
                    item['last_complete_candle_utc']=closed
            else:item['errors']['4h']='insufficient contiguous 1h candles for complete UTC-aligned 4h bar'
            watches=[]; alerts=[]; details=[]
            groups={}
            for tf,d in bars.items():groups.setdefault(d.attrs.get('proxy_source',source),{})[tf]=d
            prior=previous.get('active_boll',{})
            remaining={k:v for k,v in prior.items() if v['name']==name}
            active_all={}; ended_all=[]
            for src,subset in groups.items():
                ds=[]; ws,_,aa=evaluate(name,subset,ds)
                for x in ws+aa+ds:x['proxy_source']=src
                for w in ws:
                    if w.get('double_pattern'):w['double_pattern']['proxy_source']=src
                if ' etf ' in src or ' cfd ' in src or ' twelve ' in src:
                    for w in ws:w['setup_ready']=False;w['risk_note']+='；非CMC代理仅观察'
                watches.extend(ws);alerts.extend(aa);details.extend(ds)
                compatible={k:v for k,v in remaining.items() if v.get('proxy_source',dict((x[0],x[2]) for x in ASSETS).get(name))==src}
                active,ended=track_boll(name,subset,aa,ws,compatible)
                for k,v in active.items():
                    v.setdefault('proxy_source',src);active_all[k]=v
                ended_all.extend(ended)
                for k in compatible:remaining.pop(k,None)
            for k,v in remaining.items():active_all[k]={**v,'data_unverified':True}
            output['double_alerts'].extend(details)
            output['boll_alerts'].extend(alerts)
            output['active_boll'].update(active_all);output['ended_boll'].extend(ended_all)
            item['session_status']={tf:session_status(name,item['timeframe_sources'].get(tf,source),bars.get(tf),tf) for tf in ('15m','1h','4h','1d')}
            output['conflicts'].extend({'name':name,**x} for x in resolve_conflicts(watches,details))
            item['screen_reason']='有候选' if watches else '无8分候选或数据不足'
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

def pattern_key(a):
    return '|'.join(str(a.get(k,'')) for k in ('name','timeframe','direction','proxy_source','first_utc'))

def pattern_state(a):
    return a['stage']+'|'+str(bool(a.get('breakout_volume_confirmed')))

def report(data, previous=None):
    previous=previous or {}
    lines=['📊 CMC V5.8.3 交易扫描报告','🕒 '+nz(data['scan_utc'])+' 新西兰时间','', '🎯 正式交易机会','本轮暂无已核实的出手机会。']
    sections={}
    def heading(title):
        sections.setdefault(title, [])
        return sections[title]
    def direction(d):return '🟢 BUY 做多' if d=='Buy' else '🔴 SELL 做空'
    def block(title, items):
        if items:lines.extend(['','━━━━━━━━━━━━',title,'']+items)
    old_patterns=previous.get('pattern_states',{})
    pattern_states=dict(old_patterns)
    fresh_patterns=set()
    for a in data.get('double_alerts',[]):
        k=pattern_key(a); state=pattern_state(a)
        if old_patterns.get(k)!=state:fresh_patterns.add(k)
        pattern_states[k]=state
    seen=set(); shown=0; repeated=0
    watches=sorted(data['watch_candidates'],key=lambda w:(w['setup_ready'],w['score']),reverse=True)
    for w in watches:
        key=(w['name'],w['mode'],w['direction'],w['timeframe']) if w['mode'] in ('BOLL Reversal','Double Bottom/Top Reversal') else (w['name'],'other',w['direction'])
        if w['score']<8 or key in seen or w.get('conflict'):continue
        if w.get('double_pattern') and pattern_key({**w['double_pattern'],'proxy_source':w.get('proxy_source')}) not in fresh_patterns:
            repeated+=1;continue
        seen.add(key);shown+=1
        label={'Trend':'📈 趋势交易｜Trend','Double Bottom/Top Reversal':'🔄 双顶双底｜Reversal','BOLL Reversal':'🔔 布林线反转｜BOLL','Breakout':'⚡ 突破回踩｜Breakout'}.get(w['mode'],'⚡ '+w['mode'])
        out=heading(label)
        if out:out.append('')
        out.extend([f"{direction(w['direction'])}｜{w['name']}",f"周期：{w['timeframe']}｜评分：{w['score']:.1f}/10"])
        if w['setup_ready'] and '目标为理论值' not in w.get('risk_note',''):
            out += [w['reason'],f"技术入场参考 {w['reference_entry']:.8g}（{nz(w['reference_candle_close_utc'])}）",'来源：'+w['proxy_source'],f"止损 {w['reference_stop']:.8g} | TP1 {w['reference_tp1']:.8g} | TP2 {w['reference_tp2']:.8g}",f"盈亏比 {w['rr_tp1']:.2f}:1，未计成本；CMC及事件风险待核实"]
        else:
            note=w.get('risk_note','')
            import re
            match=re.search(r'([0-9.]+) ATR',note)
            reasons=[]
            if w.get('double_pattern') and w['double_pattern']['stage']!='颈线已突破':reasons.append('等待颈线突破')
            if match and float(match.group(1))>1:reasons.append('距离触发点过远，勿追单')
            if '挡住2R=True' in note:reasons.append('支撑/阻力限制目标')
            if w.get('reference_entry') and w.get('reference_stop') and w.get('stop_pct',0)>0 and '目标为理论值' in note:reasons.append('真实技术目标未核实')
            if w['rr_tp1']<2:reasons.append('中轨盈亏比不足2:1')
            out.append('来源：'+w['proxy_source'])
            out.append('👀 仅观察：'+('；'.join(reasons) or '入场条件或风险参数未达标'))
    for x in data.get('conflicts',[]):
        related=[a for a in data.get('double_alerts',[]) if a['name']==x['name'] and a['timeframe']==x['timeframe']]
        if related and not any(pattern_key(a) in fresh_patterns for a in related):
            heading('⚖️ 多空冲突').append(f"{x['name']}｜{x['timeframe']}｜持续冲突，暂不入场");continue
        out=heading('⚖️ 多空冲突')
        out.extend([f"{x['name']}｜{x['timeframe']}", '倾向：'+(direction(x['preferred'])+'观察' if x['preferred'] else '方向未定')+'；暂不入场',''])
        for side,patterns in x.get('sides',{}).items():
            for a in patterns:out.append(f"{direction(side)} {a['score']}分｜颈线 {a['neckline']:.8g}")
    if not shown:lines.append('已有候选持续观察或存在冲突；无新增普通候选。' if any(w['score']>=8 for w in watches) else '暂无8分以上技术候选。')
    if repeated:lines.append(f'{repeated}个既有双顶底候选持续观察，未重复展开。')
    for a in data.get('double_alerts',[]):
        if pattern_key(a) not in fresh_patterns:continue
        out=heading('🔄 双顶双底｜Reversal')
        if out:out.append('')
        point='底' if a['direction']=='Buy' else '顶'
        stage={'疑似第二顶底':f'⏳ 疑似第二{point}','结构确认':f'双{point}确认'}.get(a['stage'],a['stage'])
        shape='W形双底' if point=='底' else 'M形双顶'
        out.extend([f"{direction(a['direction'])}｜{a['name']}",f"{a['timeframe']}｜{shape}｜{stage}"])
        if a.get('name') in INDEX_FUTURES:out.append('来源：'+a.get('proxy_source','未知'))
        out.append(f"第一{point} {a['first_price']:.8g} ({nz(a['first_utc'])})\n第二{point} {a['second_price']:.8g} ({nz(a['second_utc'])})\n价格差 {a['difference_pct']:.3f}%")
        second='未知' if a['second_volume_ratio'] is None else f"{a['second_volume_ratio']:.2f}倍"
        if a.get('name') in INDEX_FUTURES:out.append('来源：'+a.get('proxy_source','未知'))
        out.append(f"第一{point}放量 {a['first_volume_ratio']:.2f}倍 ({nz(a['first_volume_utc'])})\n第二{point}量比 {second}\n颈线 {a['neckline']:.8g}\n突破放量 {'已确认' if a['breakout_volume_confirmed'] else '未确认'}")
    for title,items in sections.items():block(title,items)
    boll=[]
    oldkeys=set(previous.get('alert_keys',[]))
    current=[]
    for w in data.get('boll_alerts',[]):
        k='|'.join(str(w[x]) for x in ('name','direction','timeframe','reference_candle_close_utc'))
        current.append(k)
        if w['timeframe'] not in ('4h','1d') and k not in oldkeys:boll.extend([f"{direction(w['direction'])}｜{w['name']}",f"{w['timeframe']}｜等待第二根收盘反转", ""])
    for k,a in data.get('active_boll',{}).items():
        edge=a['trigger_low'] if a['direction']=='Buy' else a['trigger_high']
        label='新增' if k not in previous.get('active_boll',{}) else '持续'
        status=a['status']
        if a.get('tp2_hit'):status+='；TP2已触及'
        elif a.get('tp1_hit'):status+='；TP1已触及'
        if a.get('data_unverified'):status+='；暂无触发后完整K线，待核实'
        boll.extend([f"{direction(a['direction'])}｜{a['name']}",f"{a['timeframe']}｜{label}｜{status}",f"失效价格：{edge:.8g}", ""])
    for a in data.get('ended_boll',[]):
        boll.extend([f"❌ {direction(a['direction'])}｜{a['name']}",f"{a['timeframe']}｜已突破触发K线极值", ""])
    block('🔔 布林线持续预警｜BOLL',boll)
    index_notes=[]
    for a in data['assets']:
        if a['name'] not in INDEX_FUTURES:continue
        short={'Germany 40 - Cash':'德国40','US NDAQ 100 - Cash':'纳指100','Hong Kong 50 - Cash':'香港50'}[a['name']]
        srcs=a.get('timeframe_sources',{}); usable=[t for t in srcs if a.get('session_status',{}).get(t)=='可用']
        kinds=set('期货' if ' futures ' in srcs[t] else 'ETF' if ' etf ' in srcs[t] else 'CFD' if ' cfd ' in srcs[t] else '现货' for t in usable)
        label='/'.join(sorted(kinds)) if kinds else '暂无新数据'
        index_notes.append(short+' '+label+f'（{sum(1 for t in usable if a.get("metrics",{}).get(t) is not None)}/4周期可分析）')
    block('📋 股指简报',['｜'.join(index_notes)])
    lines.extend(['','━━━━━━━━━━━━','🌡️ 市场风险',''])
    ctx=data.get('risk_context' ,{}).get('VIX')
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
    data['notification_state']={'alert_keys':sorted(oldkeys.union(current))[-2000:],'issue_keys':issue_keys,'active_boll':data.get('active_boll',{}),'pattern_states':dict(list(pattern_states.items())[-2000:])}
    lines.append('')
    lines.append('评分非盈利概率；下单前核对CMC实时价格、点差及事件风险。')
    return '\n'.join(lines)

if __name__=='__main__':
    folder=Path('reports');folder.mkdir(exist_ok=True)
    previous={}
    try:previous=json.loads((folder/'latest.json').read_text(encoding='utf-8')).get('notification_state',{})
    except (OSError,ValueError,AttributeError):pass
    result=scan(previous); message=report(result,previous)
    (folder/'latest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    (folder/'latest.md').write_text(message,encoding='utf-8')
    print(message)
