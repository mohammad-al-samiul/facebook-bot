from pymongo import MongoClient

client = MongoClient("mongodb+srv://dbFbBot:dbFbBot@cluster0.5ylvmb9.mongodb.net/fb-bot?appName=Cluster0")
coll = client["fb-bot"]["bots"]
result = coll.update_one({"account_id": "demo_bot_001"}, {"$set": {"headless": False}})
print(f"Matched: {result.matched_count}, Modified: {result.modified_count}")
doc = coll.find_one({"account_id": "demo_bot_001"})
print(f"headless is now: {doc.get('headless')}")
