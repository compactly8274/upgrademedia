from pathlib import Path
import requests


class _Base:
    def __init__(self, base_url: str, headers: dict):
        # Strip angle brackets users sometimes paste from markdown/docs (e.g. <http://...>)
        self.base = base_url.strip().strip("<>").rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(headers)

    def _get(self, path: str, **params):
        r = self.session.get(f"{self.base}{path}", params=params, timeout=20)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, payload: dict):
        r = self.session.post(f"{self.base}{path}", json=payload, timeout=20)
        r.raise_for_status()
        return r.json()

    def _delete(self, path: str, **params):
        r = self.session.delete(f"{self.base}{path}", params=params, timeout=20)
        r.raise_for_status()


class RadarrClient(_Base):
    def __init__(self, url: str, api_key: str):
        super().__init__(url, {"X-Api-Key": api_key, "Accept": "application/json"})

    def movies(self):
        return self._get("/api/v3/movie")

    def search(self, movie_id: int):
        return self._post("/api/v3/command", {"name": "MoviesSearch", "movieIds": [movie_id]})

    def delete(self, movie_id: int):
        self._delete(f"/api/v3/movie/{movie_id}", deleteFiles="true", addImportExclusion="true")


class SonarrClient(_Base):
    def __init__(self, url: str, api_key: str):
        super().__init__(url, {"X-Api-Key": api_key, "Accept": "application/json"})

    def series(self):
        return self._get("/api/v3/series")

    def episodes(self, series_id: int):
        return self._get("/api/v3/episode", seriesId=series_id)

    def search_series(self, series_id: int):
        return self._post("/api/v3/command", {"name": "SeriesSearch", "seriesId": series_id})

    def search_episode(self, ep_ids: list):
        return self._post("/api/v3/command", {"name": "EpisodeSearch", "episodeIds": ep_ids})

    def delete(self, series_id: int):
        self._delete(f"/api/v3/series/{series_id}", deleteFiles="true", addImportListExclusion="true")


class PlexClient(_Base):
    def __init__(self, url: str, token: str):
        super().__init__(url, {"X-Plex-Token": token, "Accept": "application/json"})

    def sections(self):
        return self._get("/library/sections")["MediaContainer"].get("Directory", [])

    def all_items(self, key: str, type_: int):
        return self._get(f"/library/sections/{key}/all", type=type_)["MediaContainer"].get("Metadata", [])

    def delete_item(self, rating_key: str):
        self._delete(f"/library/metadata/{rating_key}")
