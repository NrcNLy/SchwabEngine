
import os
from google import genai
from dotenv import load_dotenv

load_dotenv()
api_key = os.getenv("GEMINI_API_KEY")

try:
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model="gemini-3.1-pro-preview",
        contents="Respond with exactly three words: CREDITS ARE ACTIVE."
    )
    print(f"SUCCESS: {response.text.strip()}")
except Exception as e:
    print(f"FAILED: {e}")

