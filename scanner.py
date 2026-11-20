"""Public-data CMC proxy scanner. Research only, never places trades.
No data = no signal. Never equates public reference prices with CMC CFD quotes.
"""
import json, os, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
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

def evaluate(name,bars):
    # Strict preliminary *watchlist* only. No automatic executable entries without
    # verified spread, trading hours, major events, and the actual CMC quote.
    a=metrics(bars.get('1h'));b=metrics(bars.get('1d'))
    if not a or not b:return None
    up=a['close']>a['ema20']>a['ema50'] and b['close']>b['ema20']>b['ema50']
    down=a['close']<a['ema20']<a['ema50'] and b['close']<b['ema20']<b['ema50']
    if up and a['close']>a['resistance20'] and a['rsi14'] is not None and 50<a['rsi14']<75:
        return {'direction':'Buy','reason':'1h/日线EMA多头排列，1h收盘突破此前20根高点',
                'confirmation':'等待突破位回踩站稳并核对CMC点差及事件风险'}
    if down and a['close']<a['support20'] and a['rsi14'] is not None and 25<a['rsi14']<50:
        return {'direction':'Sell','reason':'1h/日线EMA空头排列，1h收盘跌破此前20根低点',
                'confirmation':'等待跌破位反抽受阻并核对CMC点差及事件风险'}
    return None

def scan():
    output={'scan_utc':iso(NOW),'timezone':'UTC','type':'public_proxy_research_only',
            'cmc_quotes_verified':False,'high_quality_signals':[], 'assets':[], 'watch_candidates':[]}
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
                    df,err=yahoo(symbol,tf,{'15m':'5d','1h':'1mo','1d':'3mo'}[tf])
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
                item['metrics']['4h']=metrics(four)
                closed=iso(four.index[-1].to_pydatetime()+timedelta(hours=4))
                if not item['last_complete_candle_utc'] or closed>item['last_complete_candle_utc']:
                    item['last_complete_candle_utc']=closed
            else:item['errors']['4h']='insufficient contiguous 1h candles for complete UTC-aligned 4h bar'
            item['watch']=evaluate(name,bars)
            if item['watch']:output['watch_candidates'].append({'name':name,**item['watch']})
        output['assets'].append(item)
    # Intentionally no executable signals until CMC spread, events, and R:R are verified.
    return output

def report(data):
    lines=[f"# CMC 10标的扫描 — {data['scan_utc']}",'',
           '**本轮没有符合条件的出手机会。**','',
           '公开参考行情不等于CMC实时CFD买卖价；没有核实点差、重大事件及至少2:1盈亏比，不发可执行信号。','',
           '| 标的 | 15m | 1h | 4h | 1d | 最新完整K线收盘(UTC) | 来源/失败原因 |',
           '|---|:---:|:---:|:---:|:---:|---|---|']
    for a in data['assets']:
        marks=['✓' if a['timeframes'].get(tf) else '—' for tf in ['15m','1h','4h','1d']]
        errs='; '.join(f'{k}: {v}' for k,v in a['errors'].items())[:160]
        lines.append('| '+' | '.join([a['name'],*marks,a['last_complete_candle_utc'] or '未取得',
                                   (a['proxy_source']+'; '+errs).replace('|','/')])+' |')
    lines+=['','## 观察候选']
    if data['watch_candidates']:
        w=data['watch_candidates'][0]
        lines.append(f"{w['name']} — {w['direction']}；{w['reason']}；待确认：{w['confirmation']}。**仅观察，暂不进场。**")
    else:lines.append('未能选出有足够证据的观察候选。**仅观察，暂不进场。**')
    lines+=['','下单前核对CMC实时价格及点差。','']
    return '\n'.join(lines)

if __name__=='__main__':
    result=scan();folder=Path('reports');folder.mkdir(exist_ok=True)
    (folder/'latest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    (folder/'latest.md').write_text(report(result),encoding='utf-8')
    print(report(result))
 
