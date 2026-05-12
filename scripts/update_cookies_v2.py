from pathlib import Path
from pymongo import MongoClient

def parse_cookies(line: str) -> list[dict[str, str]]:
    cookies = []
    for pair in line.strip().split(";"):
        pair = pair.strip()
        if pair and "=" in pair:
            name, value = pair.split("=", 1)
            cookies.append({
                "name": name.strip(),
                "value": value.strip(),
                "domain": ".facebook.com",
                "path": "/",
            })
    return cookies

cookies_path = Path(__file__).resolve().parent.parent / "cookies.txt"
content = cookies_path.read_text(encoding="utf-8")
lines = content.splitlines()
cookie_line = None
for line in lines:
    s = line.strip()
    if s and not s.isdigit():
        cookie_line = s
        break

cookies = parse_cookies(cookie_line) if cookie_line else []

client = MongoClient("mongodb+srv://dbFbBot:dbFbBot@cluster0.5ylvmb9.mongodb.net/fb-bot?appName=Cluster0")
coll = client["fb-bot"]["bots"]
result = coll.update_one({"account_id": "demo_bot_001"}, {"$set": {"cookies": cookies}})
print(f"Matched: {result.matched_count}, Modified: {result.modified_count}, Cookies count: {len(cookies)}")
print("Sample cookie:", cookies[0] if cookies else "none")
