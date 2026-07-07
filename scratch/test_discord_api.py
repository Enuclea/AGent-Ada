import urllib.request
import json

def test_discord_api():
    print("Probing Discord API...")
    try:
        # Probe channels
        with urllib.request.urlopen("http://127.0.0.1:8090/api/discord/channels") as response:
            channels = json.loads(response.read().decode())
            print(f"Success: Discovered {len(channels.get('channels', []))} channels.")
            
        # Probe messages
        with urllib.request.urlopen("http://127.0.0.1:8090/api/discord/messages?channel=lacie&limit=1") as response:
            messages = json.loads(response.read().decode())
            print(f"Success: Fetched last message content: {messages.get('messages', [{}])[0].get('content')}")
    except Exception as e:
        print(f"Verification Failed: {e}")

if __name__ == "__main__":
    test_discord_api()
