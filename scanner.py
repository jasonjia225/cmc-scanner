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
# Only these ten may appear as candidates.
ASSETS = [
 ('US NDAQ 100 - Cash','^NDX','Yahoo Finance NDX cash index proxy'),
 ('Bitcoin (USD)',None,'Coinbase BTC-USD spot'),
 ('Ethereum (USD)',None,'Coinbase ETH-USD spot'),
 ('Hong Kong 50 - Cash','^HSI','Yahoo Finance Hang Seng cash index proxy'),
 ('China A50 - current verified CMC contract',None,'CMC contract not verified'),
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
            df=pd.DataFrame({k:q[k] for k in ['open','high','low','close']},index=pd.to_datetime(ts,unit='s',utc=True))
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

def aggregate_4h(hourly):
    if hourly is None or hourly.empty:return None
    # UTC 4h buckets; accept only four strictly consecutive hourly candles.
    groups=[]
    for t,g in hourly.groupby(hourly.index.floor('4h')):
        expected=pd.date_range(t,periods=4,freq='h',tz='UTC')
        if len(g)==4 and g.index.equals(expected):
            groups.append((t,g.open.iloc[0],g.high.max(),g.low.min(),g.close.iloc[-1]))
    if not groups:return None
    return pd.DataFrame(groups,columns=['time','open','high','low','close']).set_index('time')

def metrics(df):
    if df is None or len(df)<60:return None
    c=df.close; h=df.high; l=df.low
    ema20=c.ewm(span=20,adjust=False).mean();ema50=c.ewm(span=50,adjust=False).mean()
    d=c.diff(); gain=d.clip(lower=0).ewm(alpha=1/14,adjust=False,min_periods=14).mean()
    loss=(-d.clip(upper=0)).ewm(alpha=1/14,adjust=False,min_periods=14).mean()
    rs=gain/loss.replace(0,np.nan);rsi=100-100/(1+rs)
    tr=pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    atr=tr.ewm(alpha=1/14,adjust=False,min_periods=14).mean()
    return {'close':float(c.iloc[-1]),'ema20':float(ema20.iloc[-1]),
            'ema50':float(ema50.iloc[-1]),'rsi14':float(rsi.iloc[-1]) if pd.notna(rsi.iloc[-1]) else None,
            'atr14':float(atr.iloc[-1]),'support20':float(l.iloc[-21:-1].min()),
            'resistance20':float(h.iloc[-21:-1].max()),'bars':len(df)}

def iso(dt):return dt.isoformat().replace('+00:00','Z')

def evaluate(name, bars):
    m = {tf: metrics(bars.get(tf)) for tf in ('15m','1h','4h','1d')}
    for tf, hours in [('15m', .75), ('1h', 3), ('4h', 12), ('1d', 96)]:
        if m[tf] is not None:
            end = bars[tf].index[-1].to_pydatetime() + timedelta(seconds=14400 if tf=='4h' else TF[tf])
            if not 0 <= (NOW-end).total_seconds() <= hours*3600:
                m[tf] = None
    if any(m[t] is None for t in ('15m','1h','1d')):
        return None, '15分钟、1小时或日线样本不足/过期（至少60根）'
    a,b = m['1h'],m['15m']
    if a['atr14'] <= 0 or a['close'] <= 0:
        return None, 'ATR或价格无效'
    available = [v for v in m.values() if v is not None]
    candidates=[]
    for direction,sign in [('Buy',1),('Sell',-1)]:
        aligned=sum(sign*(v['ema20']-v['ema50'])>0 and sign*(v['close']-v['ema20'])>0 for v in available)
        breakout=sign*(a['close']-(a['resistance20'] if sign==1 else a['support20']))>0
        momentum=a['rsi14'] is not None and (52<=a['rsi14']<=72 if sign==1 else 28<=a['rsi14']<=48)
        if aligned<2: continue
        score=round(2+aligned*1.25+1.5*breakout+1.0*momentum+0.5*(sign*(b['close']-b['ema20'])>0),1)
        entry=a['close']; atr=a['atr14']
        stop=min(a['support20'],entry-1.2*atr)-.15*atr if sign==1 else max(a['resistance20'],entry+1.2*atr)+.15*atr
        risk=abs(entry-stop)
        valid=0<risk<=4*atr and min(stop,entry+sign*3*risk)>0
        end=bars['1h'].index[-1].to_pydatetime()+timedelta(hours=1)
        w=dict(direction=direction,score=score,reason=f'趋势同向{aligned}/{len(available)}周期；1h突破：'+('是' if breakout else '否')+'；1h RSI：'+(f"{a['rsi14']:.1f}" if a['rsi14'] is not None else '缺失'),confirmation='等待1小时突破及回踩确认；核对CMC价格、点差及事件风险',reference_candle_close_utc=iso(end),reference_price=entry,proxy_only=True,setup_ready=bool(valid and len(available)==4 and aligned==4 and breakout and momentum and score>=8),risk_note='结构止损过远或目标无效' if not valid else '未核实目标路径、点差及事件')
        if valid: w.update(reference_entry=entry,reference_stop=stop,reference_tp1=entry+sign*2*risk,reference_tp2=entry+sign*3*risk,rr_tp1=2.0,rr_tp2=3.0)
        candidates.append(w)
    if not candidates:return None,'多周期趋势未形成至少两个同向周期'
    candidates.sort(key=lambda w:w['score'],reverse=True)
    if len(candidates)>1 and candidates[0]['score']==candidates[1]['score']:return None,'多空评分相同'
    return candidates[0],None

def scan():
    output={'scan_utc':iso(NOW),'timezone':'UTC','type':'public_proxy_research_only',
            'cmc_quotes_verified':False,'high_quality_signals':[], 'assets':[], 'watch_candidates':[], 'research_setups':[]}
    for name,symbol,source in ASSETS:
        item={'name':name,'proxy_source':source,'currency':'USD' if 'Bitcoin' in name or 'Ethereum' in name else None,
              'timeframes':{},'last_complete_candle_utc':None,'errors':{},'metrics':{},'watch':None}
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
                    if not item['last_complete_candle_utc'] or closed>item['last_complete_candle_utc']:
                        item['last_complete_candle_utc']=closed
                else:item['timeframes'][tf]=False
                if err:item['errors'][tf]=err
            four=aggregate_4h(bars.get('1h'))
            item['timeframes']['4h']=four is not None and not four.empty
            if four is not None and not four.empty:
                bars['4h']=four
                item['metrics']['4h']=metrics(four)
                closed=iso(four.index[-1].to_pydatetime()+timedelta(hours=4))
                if not item['last_complete_candle_utc'] or closed>item['last_complete_candle_utc']:
                    item['last_complete_candle_utc']=closed
            else:item['errors']['4h']='insufficient contiguous 1h candles for complete UTC-aligned 4h bar'
            item['watch'],item['screen_reason']=evaluate(name,bars)
            if item['watch']:
                candidate={'name':name,'proxy_source':source,**item['watch']}
                output['watch_candidates'].append(candidate)
                if candidate['setup_ready']:
                    output['research_setups'].append(candidate)
        output['assets'].append(item)
    output['watch_candidates'].sort(key=lambda x:x['score'], reverse=True)
    output['research_setups'].sort(key=lambda x:x['score'], reverse=True)
    # Never mark executable high-quality signals without verified CMC spread/events.
    return output

def nz(s):
    if not s:return '无'
    return datetime.fromisoformat(s.replace('Z','+00:00')).astimezone(ZoneInfo('Pacific/Auckland')).strftime('%m-%d %H:%M')

NAMES=['纳指100','比特币','以太坊','恒生指数','中国A50','美元/日元','纽元/美元','黄金','白银','WTI原油']
SHORT=dict(zip([x[0] for x in ASSETS],NAMES))
SOURCES=['Yahoo指数','Coinbase现货','Coinbase现货','Yahoo指数','合约未核实','Yahoo外汇','Yahoo外汇','Yahoo黄金期货','Yahoo白银期货','Yahoo原油期货']

def report(data):
    lines=['CMC V3 | '+nz(data['scan_utc'])+' 新西兰时间','本轮没有符合条件的出手机会。','CMC点差、交易时段及重大事件未核实。','', '🎯 最接近的观察候选']
    if data['watch_candidates']:
        w=data['watch_candidates'][0]
        lines += [SHORT[w['name']]+' | '+w['direction']+' | 规则评分 '+str(w['score'])+'/10',w['reason'],f"1h参考价 {w['reference_price']:.8g}（{nz(w['reference_candle_close_utc'])}）",'来源：'+SOURCES[list(SHORT).index(w['name'])]]
        if 'reference_entry' in w:
            lines += [f"假设入场 {w['reference_entry']:.8g} | 止损 {w['reference_stop']:.8g}",f"TP1 {w['reference_tp1']:.8g} | TP2 {w['reference_tp2']:.8g}",'理论盈亏比 2:1 / 3:1，未计成本']
        lines += [w['risk_note'],w['confirmation'],'仅观察，暂不进场。']
    else:lines+=['未能选出可靠候选。详见各标的筛选原因。']
    lines+=['','📈 数据覆盖（15m / 1h / 4h / 日线）']
    for a,source in zip(data['assets'],SOURCES):
        marks=' '.join('✓' if a['timeframes'].get(t) else '—' for t in ('15m','1h','4h','1d'))
        lines.append(f"{SHORT[a['name']]} {marks} | {nz(a['last_complete_candle_utc'])} | {source}")
        notes=[]
        if a.get('screen_reason'):notes.append(a['screen_reason'])
        if not a['timeframes'].get('4h') and not a['name'].startswith('China'):notes.append('4h连续K线不足')
        if a['errors'] and not notes and not a['name'].startswith('China'):notes.append('部分数据异常，详见JSON')
        if notes:lines.append('  '+ '；'.join(notes))
    lines+=['','评分不是盈利概率；代理价格不等于CMC报价。','下单前核对CMC实时价格及点差。']
    return '\n'.join(lines)

if __name__=='__main__':
    result=scan();folder=Path('reports');folder.mkdir(exist_ok=True)
    (folder/'latest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    (folder/'latest.md').write_text(report(result),encoding='utf-8')
    print(report(result))
 
