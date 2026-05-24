import os

class Settings:
    def __init__(self):
        self.radarr_url = os.getenv("RADARR_URL", "http://192.168.1.x:7878")
        self.radarr_api_key = os.getenv("RADARR_API_KEY", "")
        self.sonarr_url = os.getenv("SONARR_URL", "http://192.168.1.x:8989")
        self.sonarr_api_key = os.getenv("SONARR_API_KEY", "")
        self.plex_url = os.getenv("PLEX_URL", "http://192.168.1.x:32400")
        self.plex_token = os.getenv("PLEX_TOKEN", "")
        self.db_path = os.getenv("DB_PATH", "/data/medialyze.db")
        self.quality_threshold = float(os.getenv("QUALITY_THRESHOLD", "60"))
        self.min_size_gb = float(os.getenv("MIN_SIZE_GB", "0.5"))
        self.min_days_stale = int(os.getenv("MIN_DAYS_STALE", "90"))
        self.webhook_url = os.getenv("WEBHOOK_URL", "")
        self.webhook_type = os.getenv("WEBHOOK_TYPE", "discord")
        self.media_paths = os.getenv("MEDIA_PATHS", "")

settings = Settings()
