import asyncio
import datetime
import os
import sys
import traceback
import logging
from asyncio import Task
from typing import Optional

import dateutil.parser
from gpuinfo import GPUInfo

from commons import BINARY_PATH
from recorder_config import RecoderRoom


async def async_wait_output(command):
    logging.debug("running: %s", command)
    process = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    return_value = await process.communicate()
    sys.stdout.flush()
    sys.stderr.flush()
    return return_value


class Video:
    base_path: str
    session_id: str
    video_length: float
    room_id: int
    video_resolution: str
    video_resolution_x: int
    video_resolution_y: int
    video_length_flv: float

    def __init__(self, file_closed_event_json):
        flv_name = file_closed_event_json['EventData']['RelativePath']
        self.base_path = os.path.abspath(flv_name.rpartition('.')[0])
        self.session_id = file_closed_event_json["EventData"]["SessionId"]
        self.room_id = file_closed_event_json["EventData"]["RoomId"]
        self.video_length = file_closed_event_json["EventData"]["Duration"]

    def flv_file_path(self):
        return self.base_path + ".flv"

    def xml_file_path(self):
        return self.base_path + ".xml"

    async def gen_thumbnail(self, he_time, png_file_path, video_log_path):
        ffmpeg_command_img = f"ffmpeg -y -ss {he_time} -i \"{self.flv_file_path()}\" -vframes 1 \"{png_file_path}\"" \
                             f" >> \"{video_log_path}\" 2>&1"
        await async_wait_output(ffmpeg_command_img)

    async def query_meta(self):
        video_length_str = await async_wait_output(
            f'ffprobe -v error -show_entries format=duration '
            f'-of default=noprint_wrappers=1:nokey=1 "{self.flv_file_path()}"'
        )
        video_resolution_str = await async_wait_output(
            f'ffprobe -v error -select_streams v:0 -show_entries stream=width,height '
            f'-of csv=s=x:p=0 "{self.flv_file_path()}"'
        )
        self.video_length_flv = float(video_length_str[0].decode('utf-8').strip())
        self.video_resolution = str(video_resolution_str[0].decode('utf-8').strip())
        video_resolutions = self.video_resolution.split("x")
        self.video_resolution_x, self.video_resolution_y = int(video_resolutions[0]), int(video_resolutions[1])


class Session:
    session_id: str
    start_time: datetime.datetime
    end_time: Optional[datetime.datetime]
    room_id: int
    videos: [Video]
    resolution: (int, int)
    duration: float
    he_time: Optional[float]
    room_name: str
    room_title: str
    room_area_name: (str, str)
    room_config: RecoderRoom
    prepared: bool

    def __init__(self, session_start_event_json, room_config=None):
        if room_config is None:
            self.room_config = RecoderRoom({})
        else:
            self.room_config = room_config
        self.start_time = dateutil.parser.isoparse(session_start_event_json["EventTimestamp"])
        event_data = session_start_event_json["EventData"]
        self.session_id = event_data["SessionId"]
        self.room_id = event_data["RoomId"]
        self.room_name = event_data["Name"]
        self.room_title = event_data["Title"]
        self.room_area_name = event_data["AreaNameParent"], event_data["AreaNameChild"]
        self.end_time = None
        self.videos = []
        self.resolution = 0, 0
        self.duration = 0.0
        self.he_time = None
        self.upload_task: Optional[Task] = None
        self.prepared = False

    def process_update(self, update_json):
        event_data = update_json["EventData"]
        self.room_name = event_data["Name"]
        self.room_title = event_data["Title"]
        self.room_area_name = event_data["AreaNameParent"], event_data["AreaNameChild"]
        if update_json["EventType"] == "SessionEnded":
            self.end_time = dateutil.parser.isoparse(update_json["EventTimestamp"])

    async def add_video(self, video):
        try:
            await video.query_meta()
            if video.video_resolution_x == 0 or video.video_resolution_y == 0:
                raise ValueError("resolution invalid")
            w, h = self.resolution
            if w != 0 and h != 0:
                if w != video.video_resolution_x or h != video.video_resolution_y:
                    raise ValueError("unmatched resolution")
            self.duration += video.video_length_flv
            self.resolution = video.video_resolution_x, video.video_resolution_y
        except ValueError as err:
            # print(traceback.format_exc())
            logging.warn("video %s corrupted: %s", video.flv_file_path(), err)
            return
        self.videos += [video]

    def output_base_path(self):
        return self.videos[0].base_path + ".all"

    def output_path(self):
        return {
            "xml": self.output_base_path() + ".xml",
            "clean_xml": self.output_base_path() + ".clean.xml",
            "ass": self.output_base_path() + ".ass",
            "early_video": self.output_base_path() + ".mp4",
            "danmaku_video": self.output_base_path() + ".bar.mp4",
            "concat_file": self.output_base_path() + ".concat.txt",
            "thumbnail": self.output_base_path() + ".thumb.png",
            "he_graph": self.output_base_path() + ".he.png",
            "he_file": self.output_base_path() + ".he.txt",
            "he_range": self.output_base_path() + ".he_range.txt",
            "sc_file": self.output_base_path() + ".sc.txt",
            "sc_srt": self.output_base_path() + ".sc.srt",
            "he_pos": self.output_base_path() + ".he_pos.txt",
            "extras_log": self.output_base_path() + ".extras.log",
            "video_log": self.output_base_path() + ".video.log",
        }

    async def merge_xml(self):
        xmls = ' '.join(['"' + video.xml_file_path() + '"' for video in self.videos])
        danmaku_merge_command = \
            f"python3 -m danmaku_tools.merge_danmaku " \
            f"{xmls} " \
            f"--video_time \".flv\" " \
            f"--output \"{self.output_path()['xml']}\" " \
            f">> \"{self.output_path()['extras_log']}\" 2>&1"
        await async_wait_output(danmaku_merge_command)

    async def clean_xml(self):
        danmaku_clean_command = \
            f"python3 -m danmaku_tools.clean_danmaku " \
            f"{self.output_path()['xml']} " \
            f"--output \"{self.output_path()['clean_xml']}\" " \
            f">> \"{self.output_path()['extras_log']}\" 2>&1"
        await async_wait_output(danmaku_clean_command)

    async def process_xml(self):
        danmaku_extras_command = \
            f"python3 -m danmaku_tools.danmaku_energy_map " \
            f"--graph \"{self.output_path()['he_graph']}\" " \
            f"--he_map \"{self.output_path()['he_file']}\" " \
            f"--sc_list \"{self.output_path()['sc_file']}\" " \
            f"--he_time \"{self.output_path()['he_pos']}\" " \
            f"--sc_srt \"{self.output_path()['sc_srt']}\" " \
            f"--he_range \"{self.output_path()['he_range']}\" " + \
            (
                f"--user_dict \"{self.room_config.he_user_dict}\" "
                if self.room_config.he_user_dict is not None else ""
            ) + \
            (
                f"--regex_rules \"{self.room_config.he_regex_rules}\" "
                if self.room_config.he_regex_rules is not None else ""
            ) + \
            f"\"{self.output_path()['clean_xml']}\" " \
            f">> \"{self.output_path()['extras_log']}\" 2>&1"
        await async_wait_output(danmaku_extras_command)
        with open(self.output_path()['he_pos'], 'r') as file:
            he_time_str = file.readline()
            self.he_time = float(he_time_str)

    def generate_concat(self):
        concat_text = "\n".join([f"file '{video.flv_file_path()}'" for video in self.videos])
        with open(self.output_path()['concat_file'], 'w') as concat_file:
            concat_file.write(concat_text)

    async def process_thumbnail(self):
        local_he_time = self.he_time
        thumbnail_generated = False
        for video in self.videos:
            if local_he_time < video.video_length_flv:
                await video.gen_thumbnail(local_he_time, self.output_path()['thumbnail'],
                                          self.output_path()['video_log'])
                thumbnail_generated = True
                break
            local_he_time -= video.video_length_flv
        if not thumbnail_generated:  # Rare case where he_pos is after the last video
            await self.videos[-1].gen_thumbnail(
                self.videos[-1].video_length_flv / 2,
                self.output_path()['thumbnail'],
                self.output_path()['video_log']
            )

    async def process_danmaku(self):
        video_res_x, video_res_y = self.resolution
        font_size = max(video_res_x, video_res_y) * 55 // 1920
        danmaku_conversion_command = \
            f"{BINARY_PATH}DanmakuFactory/DanmakuFactory " \
            f"-x {video_res_x} " \
            f"-y {video_res_y} " \
            f"--ignore-warnings " \
            f"-o \"{self.output_path()['ass']}\" " \
            f"-i \"{self.output_path()['clean_xml']}\" " \
            f"--fontname \"Noto Sans CJK SC\" -S {font_size} -O 255 -L 1 -D 1 --showusernames true --showmsgbox false" \
            f">> \"{self.output_path()['extras_log']}\" 2>&1"
        await async_wait_output(danmaku_conversion_command)

    async def process_early_video(self):
        ffmpeg_command = f'''ffmpeg -y \
        -f concat \
        -safe 0 \
        -i "{self.output_path()['concat_file']}" \
        -c copy "{self.output_path()['early_video']}" >> "{self.output_path()["video_log"]}" 2>&1'''
        await async_wait_output(ffmpeg_command)

    async def process_video(self):
        total_time = self.duration
        max_size = 8000_000 * 8  # Kb
        audio_bitrate = 320
        video_bitrate = (max_size / total_time - audio_bitrate) - 500  # just to be safe
        max_video_bitrate = float(8000)  # BiliBili now re-encode every video anyways
        video_bitrate = int(min(max_video_bitrate, video_bitrate))
        video_res_x, video_res_y = self.resolution
        ffmpeg_command = f'''ffmpeg -y -loop 1 -t {total_time} \
        -i "{self.output_path()['he_graph']}" \
        -f concat \
        -safe 0 \
        -i "{self.output_path()['concat_file']}" \
        -t {total_time} \
        -filter_complex "
        [1:v]scale={video_res_x}:{video_res_y}:force_original_aspect_ratio=decrease,pad={video_res_x}:{video_res_y}:-1:-1:color=black[v_fixed];
        [0:v][v_fixed]scale2ref=iw:iw*(main_h/main_w)[color][ref];
        [color]split[color1][color2];
        [color1]hue=s=0[gray];
        [color2]negate=negate_alpha=1[color_neg];
        [gray]negate=negate_alpha=1[gray_neg];
        color=black:d={total_time}[black];
        [black][ref]scale2ref[blackref][ref2];
        [blackref]split[blackref1][blackref2];
        [color_neg][blackref1]overlay=x=t/{total_time}*W-W[color_crop_neg];
        [gray_neg][blackref2]overlay=x=t/{total_time}*W[gray_crop_neg];
        [color_crop_neg]negate=negate_alpha=1[color_crop];
        [gray_crop_neg]negate=negate_alpha=1[gray_crop];
        [ref2][color_crop]overlay=y=main_h-overlay_h[out_color];
        [out_color][gray_crop]overlay=y=main_h-overlay_h[out];
        [out]ass='{self.output_path()['ass']}'[out_sub]" \
        -map "[out_sub]" -map 1:a ''' + \
                         (" -c:v h264_nvenc -preset slow "
                          if GPUInfo.check_empty() is not None else " -c:v libx264 -preset medium ") + \
                         f'-b:v {video_bitrate}K' + f''' -b:a 320K -ar 44100  "{self.output_path()['danmaku_video']}" \
                    ''' + f'>> "{self.output_path()["video_log"]}" 2>&1'
        await async_wait_output(ffmpeg_command)

    async def prepare(self):
        if len(self.videos) == 0:
            logging.warn("no videos in session %s", self.session_id)
            return
        await self.merge_xml()
        await self.clean_xml()
        await self.process_xml()
        await self.process_danmaku()
        await self.process_thumbnail()
        self.generate_concat()
        self.prepared = True

    async def gen_early_video(self):
        if not self.prepared:
            logging.error("session %s is not prepared", self.session_id)
            return
        await self.process_early_video()

    async def gen_danmaku_video(self):
        if not self.prepared:
            logging.error("session %s is not prepared", self.session_id)
            return
        await self.process_video()
