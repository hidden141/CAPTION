# Caption Cleaner Bot

Channel ke posts/files ke caption se koi bhi text/line hatane wala bot.
Kya hatana hai wo admin bot ke andar commands se khud set karta hai.

## Render par deploy
1. Ye saari files GitHub repo (private) me daalo.
2. Render -> New + -> Web Service -> repo select.
3. Build: `pip install -r requirements.txt` | Start: `python bot.py`
4. Environment Variables: API_ID, API_HASH, BOT_TOKEN, ADMINS, MONGO_URI, PYTHON_VERSION=3.11.9 (ya repo me .python-version file, jo ab included hai)
5. Bot ko channel me Admin banao (Post + Edit messages of others permission).
6. Bot ko private me /start karo.

## Commands (sirf admin, bot ke private chat me)
/addtext <text>      - jo text hatana hai add karo (multi-line bhi chalega, ya kisi message ko reply karke /addtext)
/listtext            - saved texts dekho
/deltext <no>        - number se text delete
/cleartext           - saare texts delete
/mode line|text      - line: jis line me text mile poori line hatao | text: sirf wahi text hatao
/addchannel <id>     - channel add (-100xxxxxxxxxx ya @username)
/delchannel <id>     - channel hatao
/channels            - channels list
/autoclean on|off    - naye posts auto clean
/clean [channel_id] [start_id] [end_id] - purane posts clean (end na do to automatic last post tak)
/stop                - chalti cleaning rok do
/status              - current settings

## Repair / Restore (hataye hue caption wapas)
Bot har edit se pehle post ka original caption/text save karta hai (MongoDB `backups` collection, ya local `backups.json`).
/restore [channel_id] [start_id] [end_id] - original caption wapas lagao (channel na do to saare added channels)
/backup on|off       - edit se pehle original save karna (default ON)
/backups             - kitne posts restore ho sakte hain
/clearbackup [channel_id] - backups delete (jagah bachane ke liye)

Note: sirf wahi posts restore hote hain jo is update ke BAAD clean hue (backup tabhi bana).
Pehle se clean ho chuke posts ka original bot ke paas nahi hai.
Restore ke baad dubara /clean ya autoclean chalega to text phir hat jayega - pehle /autoclean off karo.
Render par MONGO_URI zaroor set rakho, warna restart par local backups.json ud jaata hai.
