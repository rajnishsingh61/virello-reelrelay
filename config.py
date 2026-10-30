"""ReelRelay configuration. All settings are read from this file only.
Keep real credentials private; do not publish this file in a public repository.
"""
APP_PASSWORD = "RAJNISH123"
SECRET_KEY = "HXHHDDDEO6DO6DO6DOYDOYDKXK"

# Caption generation (OpenRouter or any OpenAI-compatible API)
LLM_API_KEY = "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"
LLM_BASE_URL = "https://openrouter.ai/api/v1"
LLM_MODEL = "openrouter/free"
BRAND_NAME = "Virello"

BRAND_VOICE = """
Bold, modern, energetic, creator-first, and action-oriented.
Generate authentic reaction captions based on the video's actual context.
Use engaging hooks, natural language, and relevant hashtags.
Match the video's humor, emotion, surprise, or informative nature.
Avoid robotic wording, generic viral hooks, excessive emojis,
misleading clickbait, and irrelevant hashtags.
Keep captions concise and suitable for Instagram Reels and YouTube Shorts.
"""

CREDIT_CREATOR = "yes"

# TikWM API key (get it from the TikWM API dashboard; keep server-side only)
TIKWMAPI_KEY = "35669dccb6b831cad622bb67b3a0ea40"

# Buffer is the ONLY publishing provider. Connect Instagram + YouTube channels in Buffer.
BUFFER_API_KEY = "bVVbl1tyFfSVYtnfap5E8Y1HVSRf03_XxOmFGPp75kT"
BUFFER_YOUTUBE_PRIVACY = "public"  # public | unlisted | private
TIMEZONE = "Asia/Kolkata"

# Public base URL of this app (Render URL). Example: https://virello-xxxx.onrender.com
# Required so Buffer can download the video from your own server instead of Catbox.
PUBLIC_BASE_URL = "https://virello-reelrelay.onrender.com"

