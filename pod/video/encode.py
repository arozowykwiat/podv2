from django.conf import settings
from django.core.mail import send_mail
from django.core.mail import mail_admins
from django.core.mail import mail_managers
from django.utils.translation import ugettext_lazy as _
from django.core.files.images import ImageFile
from django.core.files import File
from django.apps import apps


from pod.video.models import VideoRendition
from pod.video.models import EncodingVideo
from pod.video.models import EncodingAudio
from pod.video.models import EncodingLog
from pod.video.models import PlaylistVideo
from pod.video.models import Video
from pod.video.models import VideoImageModel
from pod.video.models import EncodingStep

from fractions import Fraction
from webvtt import WebVTT, Caption
import logging
import os
import time
import subprocess
import json
import re
import tempfile

if apps.is_installed('pod.filepicker'):
    try:
        from pod.filepicker.models import CustomImageModel
        from pod.filepicker.models import UserDirectory
    except ImportError:
        pass

FILEPICKER = True if apps.is_installed('pod.filepicker') else False

FFMPEG = getattr(settings, 'FFMPEG', 'ffmpeg')
FFPROBE = getattr(settings, 'FFPROBE', 'ffprobe')
DEBUG = getattr(settings, 'DEBUG', True)

log = logging.getLogger(__name__)

# try to create a new segment every X seconds
SEGMENT_TARGET_DURATION = getattr(settings, 'SEGMENT_TARGET_DURATION', 2)
# maximum accepted bitrate fluctuations
MAX_BITRATE_RATIO = getattr(settings, 'MAX_BITRATE_RATIO', 1.07)
# maximum buffer size between bitrate conformance checks
RATE_MONITOR_BUFFER_RATIO = getattr(
    settings, 'RATE_MONITOR_BUFFER_RATIO', 1.5)
# maximum threads use by ffmpeg
FFMPEG_NB_THREADS = getattr(settings, 'FFMPEG_NB_THREADS', 0)

GET_INFO_VIDEO = getattr(
    settings,
    'GET_INFO_VIDEO',
    "%(ffprobe)s -v quiet -show_format -show_streams -select_streams v:0 "
    + "-print_format json -i %(source)s")

FFMPEG_STATIC_PARAMS = getattr(
    settings,
    'FFMPEG_STATIC_PARAMS',
    " -c:a aac -ar 48000 -c:v h264 -profile:v high -pix_fmt yuv420p -crf 20 "
    + "-sc_threshold 0 -force_key_frames \"expr:gte(t,n_forced*1)\" "
    + "-deinterlace -threads %(nb_threads)s ")
# + "-deinterlace -threads %(nb_threads)s -g %(key_frames_interval)s "
# + "-keyint_min %(key_frames_interval)s ")

FFMPEG_MISC_PARAMS = getattr(settings, 'MISC_PARAMS', " -hide_banner -y ")

AUDIO_BITRATE = getattr(settings, 'AUDIO_BITRATE', "192k")

ENCODING_M4A = getattr(
    settings,
    'ENCODING_M4A',
    "%(ffmpeg)s -i %(source)s %(misc_params)s -c:a aac -b:a %(audio_bitrate)s "
    + "-vn -threads %(nb_threads)s "
    + "\"%(output_dir)s/audio_%(audio_bitrate)s.m4a\"")

ENCODE_MP3_CMD = getattr(
    settings, 'ENCODE_MP3_CMD',
    "%(ffmpeg)s -i %(source)s %(misc_params)s -vn -b:a %(audio_bitrate)s "
    + "-vn -f mp3 -threads %(nb_threads)s "
    + "\"%(output_dir)s/audio_%(audio_bitrate)s.mp3\"")

EMAIL_ON_ENCODING_COMPLETION = getattr(
    settings, 'EMAIL_ON_ENCODING_COMPLETION', True)

FILE_UPLOAD_TEMP_DIR = getattr(
    settings, 'FILE_UPLOAD_TEMP_DIR', '/tmp')


# function to store each step of encoding process
def change_encoding_step(video_id, num_step, desc):
    encoding_step, created = EncodingStep.objects.get_or_create(
        video=Video.objects.get(id=video_id))
    encoding_step.num_step = num_step
    encoding_step.desc_step = desc
    encoding_step.save()
    if DEBUG:
        print("step: %d - desc: %s" % (
            num_step, desc
        ))


def add_encoding_log(video_id, log):
    encoding_log, created = EncodingLog.objects.get_or_create(
        video=Video.objects.get(id=video_id))
    if encoding_log.log:
        encoding_log.log += "\n\n%s" % log
    else:
        encoding_log.log = "\n\n%s" % log
    encoding_log.save()
    if DEBUG:
        print(log)


def check_file(path_file):
    if os.access(path_file, os.F_OK) and os.stat(path_file).st_size > 0:
        return True
    return False


def remove_old_data(video_id):
    video_to_encode = Video.objects.get(id=video_id)
    encoding_log_msg = ""
    encoding_log_msg += remove_previous_encoding_video(
        video_to_encode)
    encoding_log_msg += remove_previous_encoding_audio(
        video_to_encode)
    encoding_log_msg += remove_previous_encoding_playlist(
        video_to_encode)
    return encoding_log_msg


def get_video_data(video_id):
    video_to_encode = Video.objects.get(id=video_id)
    msg = ""
    source = "%s" % video_to_encode.video.path
    command = GET_INFO_VIDEO % {'ffprobe': FFPROBE, 'source': source}
    ffproberesult = subprocess.getoutput(command)
    msg += "\nffprobe command : %s" % command
    add_encoding_log(
        video_id,
        "command : %s \n ffproberesult : %s" % (command, ffproberesult))
    info = json.loads(ffproberesult)
    msg += "%s" % json.dumps(
        info, sort_keys=True, indent=4, separators=(',', ': '))
    is_video = False
    in_height = 0
    duration = 0
    key_frames_interval = 0
    if len(info["streams"]) > 0:
        is_video = True
        if info["streams"][0].get('height'):
            in_height = info["streams"][0]['height']
        """    
        if info["streams"][0]['avg_frame_rate'] or info["streams"][0]['r_frame_rate']:
            if info["streams"][0]['avg_frame_rate'] != "0/0":
                # nb img / sec.
                frame_rate = info["streams"][0]['avg_frame_rate']
                key_frames_interval = int(round(Fraction(frame_rate)))
            else:
                frame_rate = info["streams"][0]['r_frame_rate']
                key_frames_interval = int(round(Fraction(frame_rate)))
        """
    if info["format"].get('duration'):
        duration = int(float("%s" % info["format"]['duration']))

    msg += "\nIN_HEIGHT : %s" % in_height
    msg += "\nKEY FRAMES INTERVAL : %s" % key_frames_interval
    msg += "\nDURATION : %s" % duration
    return {
        'msg': msg,
        'is_video': is_video,
        'in_height': in_height,
        'key_frames_interval': key_frames_interval,
        'duration': duration
    }


def get_video_command_mp4(video_id, video_data, output_dir):
    in_height = video_data["in_height"]
    renditions = VideoRendition.objects.filter(encode_mp4=True)
    static_params = FFMPEG_STATIC_PARAMS % {
        'nb_threads': FFMPEG_NB_THREADS,
        'key_frames_interval': video_data["key_frames_interval"]
    }
    list_file = []
    cmd = ""
    for rendition in renditions:
        resolution = rendition.resolution
        bitrate = rendition.video_bitrate
        audiorate = rendition.audio_bitrate
        encode_mp4 = rendition.encode_mp4
        width = resolution.split("x")[0]
        height = resolution.split("x")[1]
        if in_height >= int(height):
            int_bitrate = int(
                re.search("(\d+)k", bitrate, re.I).groups()[0])
            maxrate = int_bitrate * MAX_BITRATE_RATIO
            bufsize = int_bitrate * RATE_MONITOR_BUFFER_RATIO
            bandwidth = int_bitrate * 1000

            name = "%sp" % height

            cmd += " %s -vf " % (static_params,)
            cmd += "scale=w=%s:h=%s:" % (
                width, height)
            cmd += "force_original_aspect_ratio=decrease"
            cmd += " -b:v %s -maxrate %sk -bufsize %sk -b:a %s" % (
                bitrate, int(maxrate), int(bufsize), audiorate)

            cmd += " -movflags faststart -write_tmcd 0 \"%s/%s.mp4\"" % (
                output_dir, name)
            list_file.append(
                {"name": name, 'rendition': rendition})
    return {
        'cmd': cmd,
        'list_file': list_file
    }


def get_video_command_playlist(video_id, video_data, output_dir):
    in_height = video_data["in_height"]
    master_playlist = "#EXTM3U\n#EXT-X-VERSION:3\n"
    static_params = FFMPEG_STATIC_PARAMS % {
        'nb_threads': FFMPEG_NB_THREADS,
        'key_frames_interval': video_data["key_frames_interval"]
    }
    list_file = []
    cmd = ""
    renditions = VideoRendition.objects.all()
    for rendition in renditions:
        resolution = rendition.resolution
        bitrate = rendition.video_bitrate
        audiorate = rendition.audio_bitrate
        encode_mp4 = rendition.encode_mp4
        width = resolution.split("x")[0]
        height = resolution.split("x")[1]
        if in_height >= int(height):
            int_bitrate = int(
                re.search("(\d+)k", bitrate, re.I).groups()[0])
            maxrate = int_bitrate * MAX_BITRATE_RATIO
            bufsize = int_bitrate * RATE_MONITOR_BUFFER_RATIO
            bandwidth = int_bitrate * 1000

            name = "%sp" % height

            cmd += " %s -vf " % (static_params,)
            cmd += "scale=w=%s:h=%s:" % (
                width, height)
            cmd += "force_original_aspect_ratio=decrease"
            cmd += " -b:v %s -maxrate %sk -bufsize %sk -b:a %s" % (
                bitrate, int(maxrate), int(bufsize), audiorate)
            cmd += " -hls_playlist_type vod -hls_time %s \
                -hls_flags single_file %s/%s.m3u8" % (
                SEGMENT_TARGET_DURATION, output_dir, name)
            list_file.append(
                {"name": name, 'rendition': rendition})
            master_playlist += "#EXT-X-STREAM-INF:BANDWIDTH=%s,\
                RESOLUTION=%s\n%s.m3u8\n" % (
                bandwidth, resolution, name)
    return {
        'cmd': cmd,
        'list_file': list_file,
        'master_playlist': master_playlist
    }


def encode_video_playlist(source, cmd, output_dir):

    ffmpegPlaylistCommand = "%s %s -i %s %s" % (
        FFMPEG, FFMPEG_MISC_PARAMS, source, cmd)

    msg = "ffmpegPlaylistCommand :\n%s" % ffmpegPlaylistCommand
    msg += "Encoding Playlist : %s" % time.ctime()

    ffmpegvideo = subprocess.getoutput(ffmpegPlaylistCommand)

    msg += "End Encoding Playlist : %s" % time.ctime()

    with open(output_dir + "/encoding.log", "a") as f:
        f.write('\n\nffmpegvideoPlaylist:\n\n')
        f.write(ffmpegvideo)

    return msg


def encode_video_mp4(source, cmd, output_dir):

    ffmpegMp4Command = "%s %s -i %s %s" % (
        FFMPEG, FFMPEG_MISC_PARAMS, source, cmd)

    msg = "ffmpegPlaylistCommand :\n%s" % ffmpegMp4Command
    msg += "Encoding Mp4 : %s" % time.ctime()

    ffmpegvideo = subprocess.getoutput(ffmpegMp4Command)

    msg += "End Encoding Mp4 : %s" % time.ctime()

    with open(output_dir + "/encoding.log", "a") as f:
        f.write('\n\nffmpegvideoMP4:\n\n')
        f.write(ffmpegvideo)

    return msg


def save_mp4_file(video_id, list_file, output_dir):
    msg = ""
    video_to_encode = Video.objects.get(id=video_id)
    for file in list_file:
        videofilenameMp4 = os.path.join(output_dir, "%s.mp4" % file['name'])
        msg += "\n- videofilenameMp4 :\n%s" % videofilenameMp4
        if check_file(videofilenameMp4):
            encoding, created = EncodingVideo.objects.get_or_create(
                name=file['name'],
                video=video_to_encode,
                rendition=file['rendition'],
                encoding_format="video/mp4")
            encoding.source_file = videofilenameMp4.replace(
                settings.MEDIA_ROOT + '/', '')
            encoding.save()
        else:
            msg = "save_mp4_file Wrong file or path : "\
                + "\n%s " % (videofilenameMp4)
            add_encoding_log(video_id, msg)
            change_encoding_step(video_id, -1, msg)
            send_email(msg, video_id)
    return msg


def save_playlist_file(video_id, list_file, output_dir):
    msg = ""
    video_to_encode = Video.objects.get(id=video_id)
    for file in list_file:
        videofilenameM3u8 = os.path.join(output_dir, "%s.m3u8" % file['name'])
        videofilenameTS = os.path.join(output_dir, "%s.ts" % file['name'])
        msg += "\n- videofilenameM3u8 :\n%s" % videofilenameM3u8
        msg += "\n- videofilenameTS :\n%s" % videofilenameTS

        if check_file(videofilenameM3u8) and check_file(videofilenameTS):

            encoding, created = EncodingVideo.objects.get_or_create(
                name=file['name'],
                video=video_to_encode,
                rendition=file['rendition'],
                encoding_format="video/mp2t")
            encoding.source_file = videofilenameTS.replace(
                settings.MEDIA_ROOT + '/', '')
            encoding.save()

            playlist, created = PlaylistVideo.objects.get_or_create(
                name=file['name'],
                video=video_to_encode,
                encoding_format="application/x-mpegURL")
            playlist.source_file = videofilenameM3u8.replace(
                settings.MEDIA_ROOT + '/', '')
            playlist.save()
        else:
            msg = "save_playlist_file Wrong file or path : "\
                + "\n%s and %s" % (videofilenameM3u8, videofilenameTS)
            add_encoding_log(video_id, msg)
            change_encoding_step(video_id, -1, msg)
            send_email(msg, video_id)
    return msg


def save_playlist_master(video_id, output_dir, master_playlist):
    msg = ""
    playlist_master_file = output_dir + "/playlist.m3u8"
    video_to_encode = Video.objects.get(id=video_id)
    with open(playlist_master_file, "w") as f:
        f.write(master_playlist)
    if check_file(playlist_master_file):
        playlist, created = PlaylistVideo.objects.get_or_create(
            name="playlist",
            video=video_to_encode,
            encoding_format="application/x-mpegURL")
        playlist.source_file = output_dir.replace(
            settings.MEDIA_ROOT + '/', '') + "/playlist.m3u8"
        playlist.save()

        msg += "\n- Playlist :\n%s" % playlist_master_file
    else:
        msg = "save_playlist_master Wrong file or path : "\
            + "\n%s" % playlist_master_file
        add_encoding_log(video_id, msg)
        change_encoding_step(video_id, -1, msg)
        send_email(msg, video_id)
    return msg


def remove_previous_overview(overviewfilename, overviewimagefilename):
    if os.path.isfile(overviewimagefilename):
        os.remove(overviewimagefilename)
    if os.path.isfile(overviewfilename):
        os.remove(overviewfilename)


def create_overview_image(video_id, source, nb_img, image_width, overviewimagefilename):
    msg = "\ncreate overview image file"

    for i in range(0, nb_img):
        stamp = "%s" % i
        if nb_img == 99:
            stamp += "%"
        else:
            stamp = time.strftime('%H:%M:%S', time.gmtime(i))
        cmd_ffmpegthumbnailer = "ffmpegthumbnailer -t \"%(stamp)s\" \
        -s \"%(image_width)s\" -i %(source)s -c png \
        -o %(overviewimagefilename)s_strip%(num)s.png" % {
            "stamp": stamp,
            'source': source,
            'num': i,
            'overviewimagefilename': overviewimagefilename,
            'image_width': image_width
        }
        subprocess.getoutput(cmd_ffmpegthumbnailer)
        cmd_montage = "montage -geometry +0+0 %(overviewimagefilename)s \
        %(overviewimagefilename)s_strip%(num)s.png %(overviewimagefilename)s" % {
            'overviewimagefilename': overviewimagefilename,
            'num': i
        }
        subprocess.getoutput(cmd_montage)
        if os.path.isfile("%(overviewimagefilename)s_strip%(num)s.png" % {
            'overviewimagefilename': overviewimagefilename,
            'num': i
        }):
            os.remove("%(overviewimagefilename)s_strip%(num)s.png" %
                      {'overviewimagefilename': overviewimagefilename, 'num': i})
    if check_file(overviewimagefilename):
        msg += "\n- overviewimagefilename :\n%s" % overviewimagefilename
    else:
        msg = "overviewimagefilename Wrong file or path : "\
            + "\n%s" % overviewimagefilename
        add_encoding_log(video_id, msg)
        change_encoding_step(video_id, -1, msg)
        send_email(msg, video_id)


def create_overview_vtt(video_id, nb_img,
                        image_width, image_height, duration,
                        overviewfilename, image_url):
    msg = "\ncreate overview vtt file"

    # creating webvtt file
    webvtt = WebVTT()
    for i in range(0, nb_img):
        if nb_img == 99:
            start = format(float(duration * i / 100), '.3f')
            end = format(float(duration * (i + 1) / 100), '.3f')
        else:
            start = format(float(i), '.3f')
            end = format(float(i+1), '.3f')

        start_time = time.strftime(
            '%H:%M:%S',
            time.gmtime(int(str(start).split('.')[0]))
        )
        start_time += ".%s" % (str(start).split('.')[1])
        end_time = time.strftime('%H:%M:%S', time.gmtime(
            int(str(end).split('.')[0]))) + ".%s" % (str(end).split('.')[1])
        caption = Caption(
            '%s' % start_time,
            '%s' % end_time,
            '%s#xywh=%d,%d,%d,%d' % (
                image_url, image_width * i, 0, image_width, image_height)
        )
        webvtt.captions.append(caption)
    webvtt.save(overviewfilename)
    if check_file(overviewfilename):
        msg += "\n- overviewfilename :\n%s" % overviewfilename
    else:
        msg = "overviewfilename Wrong file or path : "\
            + "\n%s" % overviewfilename
        add_encoding_log(video_id, msg)
        change_encoding_step(video_id, -1, msg)
        send_email(msg, video_id)
    return msg


def save_overview_vtt(video_id, overviewfilename):
    msg = "\nstore vtt file in bdd with video model overview field"
    if check_file(overviewfilename):
        # save file in bdd
        video_to_encode = Video.objects.get(id=video_id)
        video_to_encode.overview = overviewfilename.replace(
            settings.MEDIA_ROOT + '/', '')
        video_to_encode.save()
        msg += "\n- save_overview_vtt :\n%s" % overviewfilename
    else:
        msg += "\nERROR OVERVIEW %s Output size is 0" % overviewfilename
        add_encoding_log(video_id, msg)
        change_encoding_step(video_id, -1, msg)
        send_email(msg, video_id)
    return msg

# ##########################################################################
# ##########################################################################
# ##########################################################################


def encode_video(video_id):
    start = "Start at : %s" % time.ctime()

    change_encoding_step(video_id, 0, "start")
    add_encoding_log(video_id, start)

    video_to_encode = Video.objects.get(id=video_id)
    video_to_encode.encoding_in_progress = True
    video_to_encode.save()

    if check_file(video_to_encode.video.path):

        change_encoding_step(video_id, 1, "remove old data")
        remove_msg = remove_old_data(video_id)
        add_encoding_log(video_id, "remove old data : %s" % remove_msg)

        change_encoding_step(video_id, 2, "get video data")
        video_data = {}
        try:
            video_data = get_video_data(video_id)
            add_encoding_log(video_id, "get video data : %s" %
                             video_data["msg"])
        except ValueError:
            msg = "Error in get video data"
            change_encoding_step(video_id, -1, msg)
            add_encoding_log(video_id, msg)
            send_email(msg, video_id)
            return False

        video_to_encode = Video.objects.get(id=video_id)
        video_to_encode.duration = video_data["duration"]
        video_to_encode.save()

        # create video dir
        change_encoding_step(video_id, 3, "create output dir")
        dirname = os.path.dirname(video_to_encode.video.path)
        output_dir = os.path.join(dirname, "%04d" % video_to_encode.id)
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        add_encoding_log(video_id, "output_dir : %s" % output_dir)

        change_encoding_step(video_id, 4, "encoding video file")
        if video_data["is_video"]:
            # encodage_video
            # create encoding video command
            change_encoding_step(video_id, 4,
                                 "encoding video file : get video command")
            video_command_playlist = get_video_command_playlist(
                video_id,
                video_data,
                output_dir)
            add_encoding_log(
                video_id,
                "video_command_playlist : %s" % video_command_playlist["cmd"])
            video_command_mp4 = get_video_command_mp4(
                video_id,
                video_data,
                output_dir)
            add_encoding_log(
                video_id,
                "video_command_mp4 : %s" % video_command_mp4["cmd"])
            # launch encode video
            change_encoding_step(video_id, 4,
                                 "encoding video file : encode_video_playlist")
            msg = encode_video_playlist(
                video_to_encode.video.path,
                video_command_playlist["cmd"],
                output_dir)
            add_encoding_log(
                video_id,
                "encode_video_playlist : %s" % msg)
            change_encoding_step(video_id, 4,
                                 "encoding video file : encode_video_mp4")
            msg = encode_video_mp4(
                video_to_encode.video.path,
                video_command_mp4["cmd"],
                output_dir)
            add_encoding_log(
                video_id,
                "encode_video_mp4 : %s" % msg)
            # save playlist files
            change_encoding_step(video_id, 4,
                                 "encoding video file : save_playlist_file")
            msg = save_playlist_file(
                video_id,
                video_command_playlist["list_file"],
                output_dir)
            add_encoding_log(
                video_id,
                "save_playlist_file : %s" % msg)
            # save_playlist_master
            change_encoding_step(video_id, 4,
                                 "encoding video file : save_playlist_master")
            msg = save_playlist_master(
                video_id,
                output_dir,
                video_command_playlist["master_playlist"])
            add_encoding_log(
                video_id,
                "save_playlist_master : %s" % msg)
            # save mp4 files
            change_encoding_step(video_id, 4,
                                 "encoding video file : save_mp4_file")
            msg = save_mp4_file(
                video_id,
                video_command_mp4["list_file"],
                output_dir)
            add_encoding_log(
                video_id,
                "save_mp4_file : %s" % msg)

            # get the lower size of encoding mp4
            ev = EncodingVideo.objects.filter(
                video=video_to_encode, encoding_format="video/mp4")
            video_mp4 = sorted(ev, key=lambda m: m.height)[0]

            # create overview
            overviewfilename = '%(output_dir)s/overview.vtt' % {
                'output_dir': output_dir}
            image_url = 'overview.png'
            overviewimagefilename = '%(output_dir)s/%(image_url)s' % {
                'output_dir': output_dir, 'image_url': image_url}
            image_width = video_mp4.width / 4  # width of generate image file
            change_encoding_step(video_id, 4,
                                 "encoding video file : remove_previous_overview")
            remove_previous_overview(overviewfilename, overviewimagefilename)
            if video_data["duration"] > 99:
                nb_img = 99
            else:
                nb_img = video_data["duration"]
            change_encoding_step(video_id, 4,
                                 "encoding video file : create_overview_image")
            msg = create_overview_image(
                video_id,
                video_mp4.video.video.path,
                nb_img, image_width, overviewimagefilename)
            add_encoding_log(
                video_id,
                "create_overview_image : %s" % msg)
            change_encoding_step(video_id, 4,
                                 "encoding video file : create_overview_vtt")
            overview = ImageFile(open(overviewimagefilename, 'rb'))
            image_height = int(overview.height)
            overview.close()
            image_url = os.path.basename(overviewimagefilename)
            msg = create_overview_vtt(
                video_id, nb_img, image_width, image_height,
                video_data["duration"], overviewfilename, image_url)
            add_encoding_log(
                video_id,
                "create_overview_vtt : %s" % msg)
            change_encoding_step(video_id, 4,
                                 "encoding video file : save_overview_vtt")
            msg = save_overview_vtt(video_id, overviewfilename)
            add_encoding_log(
                video_id,
                "save_overview_vtt : %s" % msg)
            # create thumbnail

        else:
            # encodage_audio_m4a
            print("encoding audio")

        # encodage_audio_mp3

        # envois mail fin encodage

        video_to_encode = Video.objects.get(id=video_id)
        video_to_encode.encoding_in_progress = False
        video_to_encode.save()

    else:
        msg = "Wrong file or path : "\
            + "\n%s" % video_to_encode.video.path
        add_encoding_log(video_id, msg)
        change_encoding_step(video_id, -1, msg)
        send_email(msg, video_id)

        #######################################################################
"""
            video_360 = EncodingVideo.objects.get(
                name="360p",
                video=video_to_encode,
                encoding_format="video/mp4")

            change_encoding_step(video_id, 2, "Encoding : create overview")
            encoding_log_msg += add_overview(data_video["duration"],
                                             video_360.source_file.path,
                                             data_video["output_dir"],
                                             video_id)

            # thumbnails
            change_encoding_step(video_id, 2, "Encoding : create thumbnails")
            encoding_log_msg += add_thumbnails(
                video_360.source_file.path,
                video_id)

        else:  # not is_video:
            change_encoding_step(video_id, 2, "Encoding : encoding audio")
            encoding_log_msg += encode_m4a(
                source,
                data_video["output_dir"],
                video_to_encode)

        # generate MP3 file for all file sent
        change_encoding_step(video_id, 3, "Encoding : encoding audio")
        encoding_log_msg += encode_mp3(
            source,
            data_video["output_dir"],
            video_to_encode)

    else:  # NOT : if os.path.exists
        encoding_log_msg += "Wrong file or path : "\
            + "\n%s" % video_to_encode.video.path
        if DEBUG:
            print("Wrong file or file path :\n%s" % video_to_encode.video.path)
        # send email Alert encodage
        send_email(encoding_log_msg, video_id)

    encoding_log.log += encoding_log_msg
    encoding_log.save()

    # SEND EMAIL TO OWNER
    if EMAIL_ON_ENCODING_COMPLETION:
        send_email_encoding(video_to_encode)

    change_encoding_step(video_id, 0, "done")

    video_to_encode = Video.objects.get(id=video_id)
    video_to_encode.encoding_in_progress = False
    video_to_encode.save()
"""


def encoding_video(source, video_encoding_cmd, data_video, video_to_encode):

    ffmpegHLScommand = "%s %s -i %s %s" % (
        FFMPEG, FFMPEG_MISC_PARAMS, source, video_encoding_cmd["cmd_hls"])
    ffmpegMP4command = "%s %s -i %s %s" % (
        FFMPEG, FFMPEG_MISC_PARAMS, source, video_encoding_cmd["cmd_mp4"])

    video_msg = "\n- ffmpegHLScommand :\n%s" % ffmpegHLScommand
    video_msg += "\n- ffmpegMP4command :\n%s" % ffmpegMP4command
    video_msg += "Encoding HLS : %s" % time.ctime()

    ffmpegvideoHLS = subprocess.getoutput(ffmpegHLScommand)

    video_msg += save_m3u8_files(
        video_encoding_cmd["list_m3u8"],
        data_video["output_dir"],
        video_to_encode,
        video_encoding_cmd["master_playlist"])

    video_msg += "\nEncoding MP4 : %s" % time.ctime()

    ffmpegvideoMP4 = subprocess.getoutput(ffmpegMP4command)

    video_msg += save_mp4_files(
        video_encoding_cmd["list_mp4"],
        data_video["output_dir"],
        video_to_encode)

    video_msg += "\nEnd Encoding video : %s" % time.ctime()

    with open(data_video["output_dir"] + "/encoding.log", "a") as f:
        f.write('\n\ffmpegvideoHLS:\n\n')
        f.write(ffmpegvideoHLS)
        f.write('\n\ffmpegvideoMP4:\n\n')
        f.write(ffmpegvideoMP4)

    if DEBUG:
        print(video_msg)

    return video_msg


def get_video_encoding_cmd(static_params, in_height, video_id, output_dir):
    msg = "\n"
    cmd_hls = ""
    cmd_mp4 = ""
    list_m3u8 = []
    list_mp4 = []

    master_playlist = "#EXTM3U\n#EXT-X-VERSION:3\n"
    renditions = VideoRendition.objects.all()
    for rendition in renditions:
        resolution = rendition.resolution
        bitrate = rendition.video_bitrate
        audiorate = rendition.audio_bitrate
        encode_mp4 = rendition.encode_mp4
        if resolution.find("x") != -1:
            width = resolution.split("x")[0]
            height = resolution.split("x")[1]
            if in_height >= int(height):

                int_bitrate = int(
                    re.search("(\d+)k", bitrate, re.I).groups()[0])
                maxrate = int_bitrate * MAX_BITRATE_RATIO
                bufsize = int_bitrate * RATE_MONITOR_BUFFER_RATIO
                bandwidth = int_bitrate * 1000

                name = "%sp" % height

                cmd = " %s -vf " % (static_params,)
                cmd += "scale=w=%s:h=%s:" % (
                    width, height)
                cmd += "force_original_aspect_ratio=decrease"
                cmd += " -b:v %s -maxrate %sk -bufsize %sk -b:a %s" % (
                    bitrate, int(maxrate), int(bufsize), audiorate)
                cmd_hls += cmd + " -hls_playlist_type vod -hls_time %s \
                    -hls_flags single_file %s/%s.m3u8" % (
                    SEGMENT_TARGET_DURATION, output_dir, name)
                list_m3u8.append(
                    {"name": name, 'rendition': rendition})

                if encode_mp4:
                    # encode only in mp4
                    cmd_mp4 += cmd + \
                        " -movflags faststart -write_tmcd 0 \"%s/%s.mp4\"" % (
                            output_dir, name)
                    list_mp4.append(
                        {"name": name, 'rendition': rendition})
                master_playlist += "#EXT-X-STREAM-INF:BANDWIDTH=%s,\
                    RESOLUTION=%s\n%s.m3u8\n" % (
                    bandwidth, resolution, name)
        else:
            msg += "\nerror in resolution %s" % resolution
            send_email(msg, video_id)
            if DEBUG:
                print("Error in resolution %s" % resolution)
    return {
        'cmd_hls': cmd_hls,
        'cmd_mp4': cmd_mp4,
        'list_m3u8': list_m3u8,
        'list_mp4': list_mp4,
        'master_playlist': master_playlist,
        'msg': msg
    }


def encode_m4a(source, output_dir, video_to_encode):
    msg = "\nEncoding M4A : %s" % time.ctime()
    command = ENCODING_M4A % {
        'ffmpeg': FFMPEG,
        'source': source,
        'misc_params': FFMPEG_MISC_PARAMS,
        'nb_threads': FFMPEG_NB_THREADS,
        'output_dir': output_dir,
        'audio_bitrate': AUDIO_BITRATE
    }
    ffmpegaudio = subprocess.getoutput(command)
    if os.access(output_dir + "/audio_%s.m4a" % AUDIO_BITRATE, os.F_OK):
        if (os.stat(output_dir + "/audio_%s.m4a" % AUDIO_BITRATE).st_size > 0):
            encoding, created = EncodingAudio.objects.get_or_create(
                name="audio",
                video=video_to_encode,
                encoding_format="video/mp4")
            encoding.source_file = output_dir.replace(
                settings.MEDIA_ROOT + '/', '')
            + "/audio_%s.m4a" % AUDIO_BITRATE
            encoding.save()
        else:
            os.remove(output_dir + "/audio_%s.m4a" % AUDIO_BITRATE)
            msg += "\nERROR ENCODING M4A audio_%s.m4a "
            +"Output size is 0" % AUDIO_BITRATE
            log.error(msg)
            send_email(msg, video_to_encode.id)
    else:
        msg += "\nERROR ENCODING M4A audio_%s.m4a "\
            + "DOES NOT EXIST" % AUDIO_BITRATE
        log.error(msg)
        send_email(msg, video_to_encode.id)

    with open(output_dir + "/encoding.log", "a") as f:
        f.write('\n\nffmpegaudio:\n\n')
        f.write(ffmpegaudio)
    if DEBUG:
        print(msg)
    return msg


def encode_mp3(source, output_dir, video_to_encode):
    msg = "\nEncoding MP3 : %s" % time.ctime()
    command = ENCODE_MP3_CMD % {
        'ffmpeg': FFMPEG,
        'source': source,
        'misc_params': FFMPEG_MISC_PARAMS,
        'nb_threads': FFMPEG_NB_THREADS,
        'output_dir': output_dir,
        'audio_bitrate': AUDIO_BITRATE
    }
    ffmpegaudiomp3 = subprocess.getoutput(command)
    if os.access(output_dir + "/audio_%s.mp3" % AUDIO_BITRATE, os.F_OK):
        if (os.stat(output_dir + "/audio_%s.mp3" % AUDIO_BITRATE).st_size > 0):
            encoding, created = EncodingAudio.objects.get_or_create(
                name="audio",
                video=video_to_encode,
                encoding_format="audio/mp3")
            encoding.source_file = output_dir.replace(
                settings.MEDIA_ROOT + '/', '')\
                + "/audio_%s.mp3" % AUDIO_BITRATE
            encoding.save()
        else:
            os.remove(output_dir + "/audio_%s.m4a" % AUDIO_BITRATE)
            msg += "\nERROR ENCODING M4A audio_%s.m4a " % AUDIO_BITRATE\
                + "Output size is 0"
            log.error(msg)
            send_email(msg, video_to_encode.id)
    else:
        msg += "\nERROR ENCODING M4A audio_%s.m4a" % AUDIO_BITRATE
        msg += " DOES NOT EXIST"
        log.error(msg)
        send_email(msg, video_to_encode.id)

    with open(output_dir + "/encoding.log", "a") as f:
        f.write('\n\ffmpegaudiomp3:\n\n')
        f.write(ffmpegaudiomp3)
    if DEBUG:
        print(msg)
    return msg

###############################################################
# THUMBNAILS
###############################################################
# nice -19 ffmpegthumbnailer -i \"%(src)s\" -s 256x256 -t 10%% -o
# %(out)s_2.png && nice -19 ffmpegthumbnailer -i \"%(src)s\" -s 256x256 -t
# 50%% -o %(out)s_3.png && nice -19 ffmpegthumbnailer -i \"%(src)s\" -s
# 256x256 -t 75%% -o %(out)s_4.png"


def add_thumbnails(source, video_id):
    msg = "\nCREATE THUMBNAILS : %s" % time.ctime()
    tempimgfile = tempfile.NamedTemporaryFile(
        dir=FILE_UPLOAD_TEMP_DIR, suffix='')
    image_width = 360  # default size of image
    msg += "\ncreate thumbnails image file"
    for i in range(0, 3):
        percent = str((i + 1) * 25) + "%"
        cmd_ffmpegthumbnailer = "ffmpegthumbnailer -t \"%(percent)s\" \
        -s \"%(image_width)s\" -i %(source)s -c png \
        -o %(tempfile)s_%(num)s.png" % {
            "percent": percent,
            'source': source,
            'num': i,
            'image_width': image_width,
            'tempfile': tempimgfile.name
        }
        subprocess.getoutput(cmd_ffmpegthumbnailer)
        thumbnailfilename = "%(tempfile)s_%(num)s.png" % {
            'num': i,
            'tempfile': tempimgfile.name
        }
        if os.access(thumbnailfilename, os.F_OK):  # outfile exists
            # There was a error cause the outfile size is zero
            if (os.stat(thumbnailfilename).st_size > 0):
                if FILEPICKER:
                    video_to_encode = Video.objects.get(id=video_id)
                    homedir, created = UserDirectory.objects.get_or_create(
                        name='Home',
                        owner=video_to_encode.owner.user,
                        parent=None)
                    videodir, created = UserDirectory.objects.get_or_create(
                        name='%s' % video_to_encode.slug,
                        owner=video_to_encode.owner.user,
                        parent=homedir)
                    thumbnail = CustomImageModel(
                        directory=videodir,
                        created_by=video_to_encode.owner.user
                    )
                    thumbnail.file.save(
                        "%d_%s.png" % (video_id, i),
                        File(open(thumbnailfilename, "rb")),
                        save=True)
                    thumbnail.save()
                else:
                    thumbnail = VideoImageModel()
                    thumbnail.file.save(
                        "%d_%s.png" % (video_id, i),
                        File(open(thumbnailfilename, "rb")),
                        save=True)
                    thumbnail.save()
                if i == 0:
                    video_to_encode = Video.objects.get(id=video_id)
                    video_to_encode.thumbnail = thumbnail
                    video_to_encode.save()
                # remove tempfile
                os.remove(thumbnailfilename)
            else:
                os.remove(thumbnailfilename)
                msg += "\nERROR THUMBNAILS %s " % thumbnailfilename
                msg += "Output size is 0"
                log.error(msg)
                send_email(msg, video_id)
        else:
            msg += "\nERROR THUMBNAILS %s DOES NOT EXIST" % thumbnailfilename
            log.error(msg)
            send_email(msg, video_id)
    if DEBUG:
        print(msg)
    return msg

###############################################################
# OVERVIEW
###############################################################


def add_overview(duration, source, output_dir, video_id):
    msg = "\nCREATE OVERVIEW : %s" % time.ctime()
    overviewfilename = '%(output_dir)s/overview.vtt' % {
        'output_dir': output_dir}
    image_url = 'overview.png'
    overviewimagefilename = '%(output_dir)s/%(image_url)s' % {
        'output_dir': output_dir, 'image_url': image_url}
    image_width = 180  # width of generate image file

    remove_old_overview_file(overviewfilename, overviewimagefilename)

    # create overviewimagefilename
    msg += "\ncreate overview image file"
    for i in range(0, 99):
        percent = "%s" % i
        percent += "%"
        cmd_ffmpegthumbnailer = "ffmpegthumbnailer -t \"%(percent)s\" \
        -s \"%(image_width)s\" -i %(source)s -c png \
        -o %(source)s_strip%(num)s.png" % {
            "percent": percent,
            'source': source,
            'num': i,
            'image_width': image_width
        }
        subprocess.getoutput(cmd_ffmpegthumbnailer)
        cmd_montage = "montage -geometry +0+0 %(output_dir)s/overview.png \
        %(source)s_strip%(num)s.png %(output_dir)s/overview.png" % {
            "percent": percent + "%",
            'source': source,
            'num': i,
            'output_dir': output_dir
        }
        subprocess.getoutput(cmd_montage)
        if os.path.isfile("%(source)s_strip%(num)s.png" % {
            'source': source,
            'num': i
        }):
            os.remove("%(source)s_strip%(num)s.png" %
                      {'source': source, 'num': i})

    # create overview vtt
    msg += "\ncreate overview vtt file"
    # get image size
    overview = ImageFile(open(overviewimagefilename, 'rb'))
    image_height = int(overview.height)
    overview.close()
    # creating webvtt file
    webvtt = WebVTT()
    for i in range(0, 99):
        start = format(float(duration * i / 100), '.3f')
        end = format(float(duration * (i + 1) / 100), '.3f')
        start_time = time.strftime(
            '%H:%M:%S',
            time.gmtime(int(str(start).split('.')[0]))
        )
        start_time += ".%s" % (str(start).split('.')[1])
        end_time = time.strftime('%H:%M:%S', time.gmtime(
            int(str(end).split('.')[0]))) + ".%s" % (str(end).split('.')[1])
        caption = Caption(
            '%s' % start_time,
            '%s' % end_time,
            '%s#xywh=%d,%d,%d,%d' % (
                image_url, image_width * i, 0, image_width, image_height)
        )
        webvtt.captions.append(caption)
    webvtt.save(overviewfilename)

    # record in Video model
    msg += "\nstore vtt file in bdd with video model overview field"
    if os.access(overviewfilename, os.F_OK):  # outfile exists
        # There was a error cause the outfile size is zero
        if (os.stat(overviewfilename).st_size > 0):
            # save file in bdd
            video_to_encode = Video.objects.get(id=video_id)
            video_to_encode.overview = overviewfilename.replace(
                settings.MEDIA_ROOT + '/', '')
            video_to_encode.save()
        else:
            os.remove(overviewfilename)
            msg += "\nERROR OVERVIEW %s Output size is 0" % overviewfilename
            log.error(msg)
            send_email(msg, video_id)
    else:
        msg += "\nERROR OVERVIEW %s DOES NOT EXIST" % overviewfilename
        log.error(msg)
        send_email(msg, video_id)

    if DEBUG:
        print(msg)

    return msg

###############################################################
# REMOVE ENCODING
###############################################################


def remove_previous_encoding_video(video_to_encode):
    msg = "\n"
    # Remove previous encoding Video
    previous_encoding_video = EncodingVideo.objects.filter(
        video=video_to_encode)
    if len(previous_encoding_video) > 0:
        msg += "\nDELETE PREVIOUS ENCODING VIDEO"
        # previous_encoding.delete()
        for encoding in previous_encoding_video:
            encoding.delete()
    else:
        msg += "Video : Nothing to delete"
    return msg


def remove_previous_encoding_audio(video_to_encode):
    msg = "\n"
    # Remove previous encoding Audio
    previous_encoding_audio = EncodingAudio.objects.filter(
        video=video_to_encode)
    if len(previous_encoding_audio) > 0:
        msg += "\nDELETE PREVIOUS ENCODING AUDIO"
        # previous_encoding.delete()
        for encoding in previous_encoding_audio:
            encoding.delete()
    else:
        msg += "Audio : Nothing to delete"
    return msg


def remove_previous_encoding_playlist(video_to_encode):
    msg = "\n"
    # Remove previous encoding Playlist
    previous_playlist = PlaylistVideo.objects.filter(video=video_to_encode)
    if len(previous_playlist) > 0:
        msg += "DELETE PREVIOUS PLAYLIST M3U8"
        # previous_encoding.delete()
        for encoding in previous_playlist:
            encoding.delete()
    else:
        msg += "Playlist : Nothing to delete"
    return msg


###############################################################
# EMAIL
###############################################################


def send_email(msg, video_id):
    subject = "[" + settings.TITLE_SITE + \
        "] Error Encoding Video id:%s" % video_id
    message = "Error Encoding  video id : %s\n%s" % (
        video_id, msg)
    html_message = "<p>Error Encoding video id : %s</p><p>%s</p>" % (
        video_id,
        msg.replace('\n', "<br/>"))
    mail_admins(
        subject,
        message,
        fail_silently=False,
        html_message=html_message)


def send_email_encoding(video_to_encode):
    if DEBUG:
        print("SEND EMAIL ON ENCODING COMPLETION")
    content_url = video_to_encode.get_absolute_url()
    subject = "[%s] %s" % (
        settings.TITLE_SITE,
        _(u"Encoding #%(content_id)s completed") % {
            'content_id': video_to_encode.id
        }
    )
    message = "%s\n\n%s\n%s\n" % (
        _(u"The content “%(content_title)s” has been encoded to Web "
            + "formats, and is now available on %(site_title)s.") % {
            'content_title': video_to_encode.title,
            'site_title': settings.TITLE_SITE
        },
        _(u"You will find it here:"),
        content_url
    )
    from_email = settings.DEFAULT_FROM_EMAIL
    to_email = []
    to_email.append(video_to_encode.owner.email)
    html_message = ""

    html_message = '<p>%s</p><p>%s<br><a href="%s"><i>%s</i></a>\
                </p>' % (
        _(u"The content “%(content_title)s” has been encoded to Web "
            + "formats, and is now available on %(site_title)s.") % {
            'content_title': '<b>%s</b>' % video_to_encode.title,
            'site_title': settings.TITLE_SITE
        },
        _(u"You will find it here:"),
        content_url,
        content_url
    )
    send_mail(
        subject,
        message,
        from_email,
        to_email,
        fail_silently=False,
        html_message=html_message,
    )
    mail_managers(
        subject, message, fail_silently=False,
        html_message=html_message)
