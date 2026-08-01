import requests

# Zoho India DC Endpoint
TOKEN_URL = "https://accounts.zoho.in/oauth/v2/token"

# Replace these 3 values with your actual credentials:
CLIENT_ID = "1000.8SKAC4MX706MT7JPCV3JA7ROHKV5QS"
CLIENT_SECRET = "bc71ed7f3804dd6b512333689e93236ce878a3b96a"
GRANT_CODE = "1000.7f58a1a72c23719d627e37bc64d19f02.8df6f88a14471de4a15dee14b1a03caf" 

payload = {
    "grant_type": "authorization_code",
    "client_id": CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "code": GRANT_CODE
}

response = requests.post(TOKEN_URL, data=payload)
data = response.json()

print("\n--- ZOHO API RESPONSE ---")
print(data)

if "refresh_token" in data:
    print("\n✅ YOUR PERMANENT REFRESH TOKEN IS:")
    print(data["refresh_token"])
else:
    print("\n❌ FAILED. Error details:", data.get("error"))