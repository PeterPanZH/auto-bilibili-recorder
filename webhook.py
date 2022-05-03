import requests


class Webhook:
    url: str

    def __init__(self, url: str):
        self.url = url

    def request(self, path: str, data: dict = {}):
        requests.post(self.url + path, json=data)

    def record_start(self, session_id: str):
        self.request("/record_start", {sessionId: session_id})

    def record_end(self, session_id: str):
        self.request("/record_end", {sessionId: session_id})

    def prepared(self, session_id: str, width: int, height: int, duration: float, thumbnail: str, danmaku: str):
        self.request("/prepared", {sessionId: session_id, width: width, height: height,
                     duration: duration, thumbnail: thumbnail, danmaku: danmaku})

    def video_generated(self, session_id: str, video_path: str):
        self.request("/video_generated",
                     {sessionId: session_id, videoPath: video_path})

    def video_transcoded(self, session_id: str, video_path: str):
        self.request("/video_transcoded",
                     {sessionId: session_id, videoPath: video_path})
