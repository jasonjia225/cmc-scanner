"""Manual verified calendar input, UTC offset required; no invented events."""
import json
from pathlib import Path
from datetime import datetime,timezone
from telegram_send import send
p=Path('events.json'); state=Path('reports/event_state.json')
def due(events, now, sent):
    result=[]
    for e in events:
        t=datetime.fromisoformat(e['time']);
        if t.tzinfo is None:raise ValueError('event timestamp must include timezone')
        key=e['name']+'|'+t.isoformat()
        minutes=(t-now).total_seconds()/60
        if 0<minutes<=30 and key not in sent and e.get('source'):result.append((key,e,minutes))
    return result
if __name__=='__main__':
    events=json.loads(p.read_text()); sent=json.loads(state.read_text()) if state.exists() else []
    for key,e,minutes in due(events,datetime.now(timezone.utc),sent):
        send(f"🚨 重大经济事件提醒\n{e['name']}\n距离公布约 {minutes:.0f} 分钟\n来源：{e['source']}\n注意点差扩大及波动风险")
        sent.append(key);state.write_text(json.dumps(sent))
