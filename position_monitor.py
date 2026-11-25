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
        if (l and not a) or (s and not b) or not math.isfinite(price) or price<=0 or multiplier<=0: raise ValueError('invalid cost or multiplier')
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

def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def previous_valid(path, now):
    # Select the newest complete, valid snapshot independently for each asset.
    found = {}
    try:
        with (path / 'history.jsonl').open(encoding='utf-8') as f:
            for line in f:
                try:
                    record = json.loads(line)
                    stamp = datetime.fromisoformat(record['retrieved_at'].replace('Z', '+00:00'))
                    if stamp.tzinfo is None or stamp >= now:
                        continue
                    for symbol in ('XAUUSD', 'BTCUSD'):
                        m = record['assets'].get(symbol, {})
                        if not isinstance(m, dict) or m.get('error'):
                            continue
                        if not all(finite(m.get(k)) for k in ('long_pnl_usd', 'short_pnl_usd', 'net_volume', 'assumed_lot_size')) or m['assumed_lot_size'] <= 0:
                            continue
                        if symbol not in found or stamp > found[symbol][0]:
                            found[symbol] = (stamp, m)
                except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                    continue
    except OSError:
        pass
    return found


def compare(m, previous):
    if previous is None:
        return None, ['较上次：暂无对比数据']
    stamp, old = previous
    d = {'previous_at': stamp.isoformat(), 'net_volume_change': None,
         'long_loss_change_usd': None, 'short_profit_change_usd': None, 'long_pnl_change_usd': None, 'short_pnl_change_usd': None}
    if finite(m.get('net_volume')):
        d['net_volume_change'] = m['net_volume'] - old['net_volume']
    if m.get('assumed_lot_size') == old['assumed_lot_size']:
        if finite(m.get('long_pnl_usd')):
            d['long_pnl_change_usd'] = m['long_pnl_usd'] - old['long_pnl_usd']
            d['long_loss_change_usd'] = max(0, -m['long_pnl_usd']) - max(0, -old['long_pnl_usd'])
        if finite(m.get('short_pnl_usd')):
            d['short_pnl_change_usd'] = m['short_pnl_usd'] - old['short_pnl_usd']
            d['short_profit_change_usd'] = max(0, m['short_pnl_usd']) - max(0, old['short_pnl_usd'])
    def fmt(value, unit, scale=1):
        return f'{value/scale:+,.2f}{unit}' if finite(value) else '暂无可比数据'
    label = stamp.astimezone(ZoneInfo('Pacific/Auckland')).strftime('%m-%d %H:%M')
    return d, [f'较上次有效报告（{label}）：',
               '多头亏损变化：' + fmt(d['long_loss_change_usd'], '万美元', 10000),
               '空头盈利变化：' + fmt(d['short_profit_change_usd'], '万美元', 10000),
               '净持仓变化：' + fmt(d['net_volume_change'], '手')]


def assess(m, d):
    # Heuristic observation only; never a price forecast or entry signal.
    l, s, pct = (m.get(k) for k in ('long_pnl_usd', 'short_pnl_usd', 'net_pct'))
    if not all(finite(x) for x in (l, s, pct)) or not d:
        return '方向不明', '盈亏或历史对比不足，暂不判断方向。'
    loss, profit, net = (d.get(k) for k in ('long_loss_change_usd', 'short_profit_change_usd', 'net_volume_change'))
    if not all(finite(x) for x in (loss, profit, net)):
        return '方向不明', '本次与历史数据不完全可比。'
    if l < 0 < s:
        if loss < 0 and profit < 0 and net > 0:
            return '关注反弹', '多头亏损收窄、空头盈利减少，净持仓向多方移动。'
        if loss > 0 and profit > 0 and net <= 0:
            return '偏空', '多头亏损扩大、空头盈利增加，净持仓未向多方移动。'
    if s < 0 < l:
        if d['long_pnl_change_usd'] < 0 and d['short_pnl_change_usd'] > 0 and net < 0:
            return '关注回落', '多头盈利收窄、空头亏损减轻，净持仓向空方移动。'
        if net > 0 and d['long_pnl_change_usd'] > 0 and d['short_pnl_change_usd'] < 0:
            return '偏多', '多头盈利扩大、空头亏损扩大，净持仓向多方移动。'
    # Symmetric pullback check uses signed P&L changes, not clipped profit/loss.
    if pct == 0 and net == 0:
        return '方向不明', '净持仓没有明显方向。'
    return '方向不明', '持仓与盈亏变化未形成一致信号。'


def main():
    now=datetime.now(timezone.utc); lines=['📊 胜利循环｜多空持仓监控',now.astimezone(ZoneInfo('Pacific/Auckland')).strftime('%m-%d %H:%M 新西兰时间')]; result={'retrieved_at':now.isoformat(),'assets':{}}
    path=Path('position_reports'); path.mkdir(exist_ok=True)
    history=previous_valid(path, now)
    try:
        rows={r['name'].upper().replace('/',''):r for r in outlook()}
    except Exception:
        rows={}; lines.append('⚠️ Myfxbook查询失败；未使用旧数据。')
    for symbol,label in [('XAUUSD','🟡 黄金'),('BTCUSD','🟠 比特币')]:
        lines.extend(['',label]); row=rows.get(symbol)
        try: m=metrics(row)
        except Exception:
            lines.extend(['持仓数据未核实', '较上次：暂无对比数据', '方向：方向不明']); result['assets'][symbol]={'error':'missing or invalid sentiment'}; continue
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
        d, change_lines = compare(m, history.get(symbol))
        direction, analysis = assess(m, d)
        lines.extend(change_lines)
        lines.extend(['分析：'+analysis, '方向：'+direction])
        m.update(comparison=d, direction=direction, analysis=analysis)
        result['assets'][symbol]=m
    lines.extend(['','Myfxbook样本估算，源更新时间未知','持仓方向分析不等同于价格预测。'])
    report='\n'.join(lines); (path/'latest.md').write_text(report,encoding='utf-8'); (path/'latest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    with (path/'history.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(result,ensure_ascii=False)+'\n')
    send(report)
if __name__=='__main__':main()
