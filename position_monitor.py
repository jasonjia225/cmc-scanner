import os, math, json, requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from telegram_send import send

def get(url, params=None, headers=None):
    try:
        r=requests.get(url,params=params,headers=headers,timeout=20)
        if not r.ok: raise RuntimeError(f'HTTP {r.status_code}')
        d=r.json()
        if isinstance(d,dict) and d.get('error') not in (None,False): raise RuntimeError('API rejected request')
        return d
    except (requests.RequestException, ValueError): raise RuntimeError('request failed') from None

def outlook():
    base='https://www.myfxbook.com/api/'
    session=get(base+'login.json', {'email':os.environ['MYFXBOOK_EMAIL'],'password':os.environ['MYFXBOOK_PASSWORD']})['session']
    try: return get(base+'get-community-outlook.json',{'session':session})['symbols']
    finally:
        try: get(base+'logout.json',{'session':session})
        except Exception: pass

def quote(symbol):
    if symbol=='BTCUSD':
        d=get('https://api.coinbase.com/v2/prices/BTC-USD/spot')['data']
        return float(d['amount']), 'Coinbase spot（查询时价格，源时间未知）'
    account=os.environ['OANDA_ACCOUNT_ID']; token=os.environ['OANDA_API_TOKEN']
    q=get(f'https://api-fxpractice.oanda.com/v3/accounts/{account}/pricing',{'instruments':'XAU_USD'},{'Authorization':'Bearer '+token})['prices'][0]
    age=(datetime.now(timezone.utc)-datetime.fromisoformat(q['time'].replace('Z','+00:00'))).total_seconds()
    if not q.get('tradeable') or not 0<=age<=300: raise RuntimeError('黄金休市或报价过期')
    return (float(q['bids'][0]['price'])+float(q['asks'][0]['price']))/2, 'OANDA practice midpoint'

def number(x):
    x=float(x)
    if not math.isfinite(x) or x<0: raise ValueError('invalid value')
    return x

def metrics(row,price=None,multiplier=None):
    l,s=number(row['longVolume']),number(row['shortVolume'])
    out={'long_volume':l,'short_volume':s,'net_volume':l-s,'net_pct':100*(l-s)/(l+s) if l+s else None,'pnl_gap_usd':None,'long_pnl_usd':None,'short_pnl_usd':None}
    if price is not None and multiplier is not None:
        a,b=number(row['avgLongPrice']),number(row['avgShortPrice'])
        if (l and not a) or (s and not b) or price<=0 or multiplier<=0: raise ValueError('invalid cost or multiplier')
        out['long_pnl_usd']=(price-a)*l*multiplier
        out['short_pnl_usd']=(b-price)*s*multiplier
        out['pnl_gap_usd']=out['long_pnl_usd']-out['short_pnl_usd']
    return out

def pnl_status(value, volume):
    if volume == 0: return '⚪ 无持仓'
    if value is None: return '⚪ 未核实'
    amount=f'{value/10000:+,.2f}万美元'
    status='🟢 盈利' if value>0 else '🔴 被套' if value<0 else '⚪ 持平'
    return f'{status} {amount}（估算）'

def main():
    now=datetime.now(timezone.utc); lines=['📊 黄金与比特币多空持仓监控',now.astimezone(ZoneInfo('Pacific/Auckland')).strftime('%m-%d %H:%M 新西兰时间')]; result={'retrieved_at':now.isoformat(),'assets':{}}
    try:
        rows={r['name'].upper().replace('/',''):r for r in outlook()}
    except Exception:
        rows={}; lines.append('⚠️ Myfxbook查询失败；未使用旧数据。')
    for symbol,label in [('XAUUSD','🟡 黄金'),('BTCUSD','🟠 比特币')]:
        lines.extend(['',label]); row=rows.get(symbol)
        try: m=metrics(row)
        except Exception:
            lines.append('持仓数据未核实'); result['assets'][symbol]={'error':'missing or invalid sentiment'}; continue
        m['quote']=None
        try:
            p,source=quote(symbol); m.update(quote=p,quote_source=source)
            # Explicit configurable assumptions, not verified platform-wide contract specs.
            mult=number(os.getenv(symbol+'_LOT_SIZE') or ('100' if symbol=='XAUUSD' else '1'))
            m.update(metrics(row,p,mult)); m['assumed_lot_size']=mult
        except Exception: pass
        gap=m['pnl_gap_usd']; pct=m['net_pct']
        lines.append('多头：'+pnl_status(m['long_pnl_usd'],m['long_volume']))
        lines.append('空头：'+pnl_status(m['short_pnl_usd'],m['short_volume']))
        lines.append('多空净盈亏差：'+(f'{gap/10000:+,.2f}万美元（估算）' if gap is not None else '未核实'))
        lines.append(f"多空净持仓量：{m['net_volume']:+,.2f}手")
        lines.append('多空净持仓比例：'+(f'{pct:+.2f}%' if pct is not None else '无持仓'))
        result['assets'][symbol]=m
    lines.extend(['','Myfxbook样本，源更新时间未知；盈亏假设每手黄金100盎司、BTC 1枚（可配置），均价按手数加权，未计费用；盈亏状态为该方向总体净浮盈亏，非所有客户均盈利或被套；非全球数据。'])
    path=Path('position_reports'); path.mkdir(exist_ok=True)
    report='\n'.join(lines); (path/'latest.md').write_text(report,encoding='utf-8'); (path/'latest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    with (path/'history.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(result,ensure_ascii=False)+'\n')
    send(report)
if __name__=='__main__':main()
