import asyncio
import os.path
import sys
import threading
import time
import traceback
from queue import Queue
from string import Template

import dateutil.parser
import yaml
import logging
from bilibili_api import video

from comment_task import CommentTask
from recorder_config import RecorderConfig, UploaderAccount
from recorder_manager import RecorderManager
from session import Session, Video
from subtitle_task import SubtitleTask
from task_save import TaskSave
from upload_task import UploadTask
from webhook import Webhook


VIDEO_UPLOAD_RETRY_TIMES = 5
DANMAKU_VIDEO_WAIT_MINUTES = 6
EARLY_VIDEO_WAIT_MINUTES = 1


class RecordUploadManager:
    def __init__(self, port, config_path, save_path):
        self.config_path = config_path
        self.save_path = save_path
        with open(config_path, 'r') as file:
            self.config = RecorderConfig(yaml.load(file, Loader=yaml.FullLoader))
        if os.path.isfile(save_path):
            with open(save_path, 'r') as file:
                self.save = TaskSave.from_dict(yaml.load(file, Loader=yaml.FullLoader))
        else:
            logging.info("creating save file to %s", save_path)
            self.save = TaskSave()
            self.save_progress()
        self.recorder_manager = RecorderManager(port, [room.id for room in self.config.rooms])
        self.sessions: {str: Session} = dict()

        self.webhooks: {int: Webhook} = dict()
        for room in self.config.rooms:
            self.webhooks[room.id] = Webhook(room)

        self.video_upload_queue: Queue[UploadTask] = Queue()
        self.comment_post_queue: Queue[CommentTask] = Queue()
        self.subtitle_post_queue: Queue[SubtitleTask] = Queue()
        self.save_lock = threading.Lock()
        self.video_upload_thread = threading.Thread(target=self.video_uploader)
        self.comment_post_thread = threading.Thread(target=self.comment_poster)
        self.subtitle_post_thread = threading.Thread(target=self.subtitle_poster)
        self.video_processing_loop = asyncio.new_event_loop()
        self.video_uploading_loop = asyncio.new_event_loop()
        self.subtitle_posting_loop = asyncio.new_event_loop()
        self.video_upload_thread.start()
        self.comment_post_thread.start()
        self.subtitle_post_thread.start()
        self.video_uploading_thread = threading.Thread(target=lambda: self.video_processing_loop.run_forever())
        self.video_uploading_thread.start()

    def save_progress(self):
        with open(self.save_path, 'w') as file:
            yaml.dump(self.save.to_dict(), file, Dumper=yaml.Dumper)

    def video_uploader(self):
        asyncio.set_event_loop(self.video_uploading_loop)
        while True:
            upload_task = self.video_upload_queue.get()
            try:
                first_video_comment = upload_task.session_id not in self.save.session_id_map
                logging.info("uploading video...")
                bv_id = upload_task.upload(self.save.session_id_map)
                with self.save_lock:
                    self.save.session_id_map[upload_task.session_id] = bv_id
                    self.save_progress()
                if first_video_comment:
                    self.comment_post_queue.put(
                        CommentTask.from_upload_task(upload_task)
                    )
                v_info = video.get_video_info(bvid=bv_id, is_simple=False, is_member=True, verify=upload_task.verify)
                cid = v_info['videos'][0]['cid']
                self.subtitle_post_queue.put(
                    SubtitleTask.from_upload_task(upload_task, bv_id, cid)
                )
            except Exception:
                if upload_task.trial < VIDEO_UPLOAD_RETRY_TIMES:
                    upload_task.trial += 1
                    self.video_upload_queue.put(upload_task)
                    logging.warn("task %s uploading failed, retrying", upload_task.title)
                else:
                    logging.error("task %s uploading failed too many times", upload_task.title)
                # print(traceback.format_exc())

    def comment_poster(self):
        asyncio.set_event_loop(self.video_uploading_loop)
        while True:
            with self.save_lock:
                while not self.comment_post_queue.empty():
                    self.save.active_comment_tasks += [self.comment_post_queue.get()]
                    self.save_progress()
            try:
                if len(self.save.active_comment_tasks) != 0:
                    task_to_remove = []
                    for idx, task in enumerate(self.save.active_comment_tasks):
                        task: CommentTask
                        logging.info("posting comment...")
                        if task.post_comment(self.save.session_id_map):
                            task_to_remove += [idx]
                    if task_to_remove != 0:
                        with self.save_lock:
                            self.save.active_comment_tasks = [
                                comment_task
                                for idx, comment_task in enumerate(self.save.active_comment_tasks)
                                if idx not in task_to_remove
                            ]
                            self.save_progress()
            except Exception:
                logging.error("comment post failed")
                # print(traceback.format_exc())
            finally:
                time.sleep(60)

    def subtitle_poster(self):
        asyncio.set_event_loop(self.subtitle_posting_loop)
        while True:
            with self.save_lock:
                while not self.subtitle_post_queue.empty():
                    self.save.active_subtitle_tasks += [self.subtitle_post_queue.get()]
                    self.save_progress()
            try:
                if len(self.save.active_subtitle_tasks) != 0:
                    task_to_remove = []
                    for idx, task in enumerate(self.save.active_subtitle_tasks):
                        task: SubtitleTask
                        logging.info("posting subtitle...")
                        if task.post_subtitle():
                            task_to_remove += [idx]
                    if task_to_remove != 0:
                        with self.save_lock:
                            new_subtitle_tasks = []
                            for idx, subtitle_task in enumerate(self.save.active_subtitle_tasks):
                                subtitle_task: SubtitleTask
                                append = True
                                if idx in task_to_remove:
                                    append = False
                                else:
                                    for j in task_to_remove:
                                        removing_task = self.save.active_subtitle_tasks[j]
                                        if subtitle_task.is_earlier_task_of(removing_task):
                                            append = False
                                            break
                                if append:
                                    new_subtitle_tasks += [subtitle_task]
                            self.save.active_subtitle_tasks = new_subtitle_tasks
                            self.save_progress()
            except Exception:
                logging.error("subtitle post failed")
                # print(traceback.format_exc())
            finally:
                time.sleep(60)

    async def session_end(self, session: Session):
        await asyncio.sleep(EARLY_VIDEO_WAIT_MINUTES * 60)
        if len(session.videos) == 0:
            logging.info("No video in session %d@%s", session.room_id, session.session_id)
            return
        room_config = session.room_config
        await asyncio.sleep(room_config.continue_session_minutes * 60)

        self.webhooks[session.room_id].record_end(
            session_id = session.session_id,
            title = session.room_title,
            name = session.room_name,
            area_name = session.room_area_name,
            time = session.end_time
        )

        await self.upload_video(session)

    async def upload_video(self, session: Session):
        webhook = self.webhooks[session.room_id]
        paths = session.output_path()

        await session.prepare()
        width, height = session.resolution
        webhook.prepared(
            session_id=session.session_id,
            width=width,
            height=height,
            duration=session.duration,
            thumbnail=paths.get('thumbnail'),
            danmaku=paths.get('xml')
        )

        await session.gen_early_video()
        webhook.video_generated(
            session_id=session.session_id,
            video_path=paths.get("early_video")
        )

        room_config = session.room_config
        uploader: UploaderAccount
        title
        description
        early_upload_task = None

        if room_config.uploader is not None:
            uploader = self.config.accounts[room_config.uploader]
            substitute_dict = {
                "name": session.room_name,
                "title": session.room_title,
                "uploader_name": uploader.name,
                "y": session.start_time.year,
                "m": session.start_time.month,
                "d": session.start_time.day,
                "yy": f"{session.start_time.year:04d}",
                "mm": f"{session.start_time.month:02d}",
                "dd": f"{session.start_time.day:02d}",
                "flv_path": session.videos[0].flv_file_path()
            }
            title = Template(room_config.title).substitute(substitute_dict)
            temp_title = title
            i = 1
            other_video_titles = [
                name for session_id, name in self.save.video_name_history.items()
                if session_id != session.session_id
            ]
            while temp_title in other_video_titles:
                i += 1
                temp_title = f"{temp_title}{i}"
            title = temp_title
            with self.save_lock:
                self.save.video_name_history[session.session_id] = title
            description = Template(room_config.description).substitute(substitute_dict)

            if session.prepared:
                early_upload_task = UploadTask(
                    session_id=session.session_id,
                    video_path=paths.get("early_video"),
                    thumbnail_path=paths.get("thumbnail"),
                    sc_path=paths.get("sc_file"),
                    he_path=paths.get("he_file"),
                    subtitle_path=paths.get("sc_srt"),
                    title=title,
                    source=room_config.source,
                    description=description,
                    tag=room_config.tags,
                    channel_id=room_config.channel_id,
                    danmaku=False,
                    account=uploader
                )
                self.video_upload_queue.put(early_upload_task)

        await asyncio.sleep(DANMAKU_VIDEO_WAIT_MINUTES * 60)
        await session.gen_danmaku_video()
        webhook.video_transcoded(
            session_id = session.session_id,
            video_path = paths.get("danmaku_video")
        )

        if room_config.uploader is not None:
            danmaku_upload_task = UploadTask(
                session_id=session.session_id,
                video_path=paths.get("danmaku_video"),
                thumbnail_path=paths.get("thumbnail"),
                sc_path=paths.get("sc_file"),
                he_path=paths.get("he_file"),
                subtitle_path=paths.get("sc_srt"),
                title=title,
                source=room_config.source,
                description=description,
                tag=room_config.tags,
                channel_id=room_config.channel_id,
                danmaku=True,
                account=uploader
            )
            self.video_upload_queue.put(
                danmaku_upload_task
            )
            if early_upload_task is None:
                self.comment_post_queue.put(
                    CommentTask.from_upload_task(danmaku_upload_task)
                )

    async def handle_update(self, update_json: dict):
        room_id = update_json["EventData"]["RoomId"]
        session_id = update_json["EventData"]["SessionId"]
        event_timestamp = dateutil.parser.isoparse(update_json["EventTimestamp"])

        room_config = None
        for room in self.config.rooms:
            if room.id == room_id:
                room_config = room
        if room_config is None:
            logging.warn("Cannot find room config for %d!", room_id)
            return

        if update_json["EventType"] == "SessionStarted":
            for session in self.sessions.values():
                if session.room_id == room_id and session.end_time is not None and \
                        (event_timestamp - session.end_time).total_seconds() / 60 < room_config.continue_session_minutes:
                    self.sessions[session_id] = session
                    if session.upload_task is not None:
                        session.upload_task.cancel()
                        session.upload_task = None
                    return

            session = Session(update_json, room_config)
            self.sessions[session_id] = session
            self.webhooks[room_id].record_start(
                session_id = session_id,
                title = session.room_title,
                name = session.room_name,
                area_name = session.room_area_name,
                time = session.start_time
            )
        else:
            if session_id not in self.sessions:
                logging.warn("session %d@%s does not exists", room_id, session_id)
                return
            current_session: Session = self.sessions[session_id]
            current_session.process_update(update_json)
            if update_json["EventType"] == "FileClosed":
                new_video = Video(update_json)
                await current_session.add_video(new_video)
            elif update_json["EventType"] == "SessionEnded":
                current_session.upload_task = \
                    asyncio.run_coroutine_threadsafe(self.session_end(current_session), self.video_processing_loop)
