import time
import datetime

from django.apps import apps
from webvtt import WebVTT
from pod.chapter.models import Chapter
if apps.is_installed('pod.podfile'):
    FILEPICKER = True


def vtt_to_chapter(vtt, video):
    Chapter.objects.filter(video=video).delete()
    if FILEPICKER:
        webvtt = WebVTT().read(vtt.file.path)
    else:
        webvtt = WebVTT().read(vtt.path)
    for caption in webvtt:
        time_start = time.strptime(caption.start.split('.')[0], '%H:%M:%S')
        time_start = datetime.timedelta(
            hours=time_start.tm_hour,
            minutes=time_start.tm_min,
            seconds=time_start.tm_sec).total_seconds()

        if time_start > video.duration or time_start < 0:
            return 'The VTT file contains a chapter started at an ' + \
                   'incorrect time in the video : {0}'.format(caption.text)

        new = Chapter()
        new.title = caption.text
        new.time_start = time_start
        new.video = video
        new.save()
