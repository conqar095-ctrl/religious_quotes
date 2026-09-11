# religious_quotes

Automated Arabic religious Shorts pipeline.

## GitHub Actions secrets

Create these repository secrets before running the workflow:

- `ELEVENLABS_API_KEY`
- `PIXABAY_API_KEY`
- `PEXELS_API_KEY`
- `YOUTUBE_TOKEN_JSON`

Do not commit `token.json`, API keys, `.env`, or the `keys/` directory.

The workflow runs `religious_quotes.py`, uploads the finished video to YouTube, then commits `data/used_voiceover_scripts.txt` so the next run remembers which ready voiceover scripts were already used.
