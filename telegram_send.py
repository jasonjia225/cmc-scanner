import os, sys, requests
from pathlib import Path

def split_message(text, limit=1800):
    parts=[]; chunk=''
    for line in text.splitlines(keepends=True):
        while len(line)>limit:
            if chunk:parts.append(chunk);chunk=''
            parts.append(line[:limit]);line=line[limit:]
        if len(chunk)+len(line)>limit:parts.append(chunk);chunk=''
        chunk+=line
    if chunk.strip():parts.append(chunk)
    return parts

def send(text):
    token=os.environ['TELEGRAM_BOT_TOKEN']; chat=os.environ['TELEGRAM_CHAT_ID']
    for part in split_message(text):
        try:
            r=requests.post(f'https://api.telegram.org/bot{token}/sendMessage',json={'chat_id':chat,'text':part},timeout=45)
            if not r.ok or not r.json().get('ok'):raise RuntimeError('Telegram rejected message')
        except requests.RequestException:raise RuntimeError('Telegram request failed') from None
if __name__=='__main__':send(Path(sys.argv[1]).read_text())
