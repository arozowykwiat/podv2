from django.db import models
from django.utils.translation import ugettext_lazy as _
from django.contrib.auth.models import User, Permission
from django.conf import settings
from django.dispatch import receiver
from django.db.models.signals import post_save

import hashlib
import logging
import traceback
logger = logging.getLogger(__name__)

if getattr(settings, 'USE_PODFILE', False):
    from pod.podfile.models import CustomImageModel
else:
    from pod.main.models import CustomImageModel

AUTH_TYPE = getattr(
    settings, 'AUTH_TYPE', (('local', _('local')), ('CAS', 'CAS')))
AFFILIATION = getattr(
    settings, 'AFFILIATION',
    (
        ('student', _('student')),
        ('faculty', _('faculty')),
        ('staff', _('staff')),
        ('employee', _('employee')),
        ('member', _('member')),
        ('affiliate', _('affiliate')),
        ('alum', _('alum')),
        ('library-walk-in', _('library-walk-in')),
        ('researcher', _('researcher')),
        ('retired', _('retired')),
        ('emeritus', _('emeritus')),
        ('teacher', _('teacher')),
        ('registered-reader', _('registered-reader'))
    )
)
SECRET_KEY = getattr(settings, 'SECRET_KEY', '')
FILES_DIR = getattr(
    settings, 'FILES_DIR', 'files')


def get_name(self):
    return '%s %s (%s)' % (self.first_name, self.last_name, self.username)


User.add_to_class("__str__", get_name)


class Owner(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    auth_type = models.CharField(
        max_length=20, choices=AUTH_TYPE, default=AUTH_TYPE[0][0])
    affiliation = models.CharField(
        max_length=50, choices=AFFILIATION, default=AFFILIATION[0][0])
    commentaire = models.TextField(_('Comment'), blank=True, default="")
    hashkey = models.CharField(
        max_length=64, unique=True, blank=True, default="")
    userpicture = models.ForeignKey(CustomImageModel,
                                    blank=True, null=True,
                                    verbose_name=_('Picture'))

    def __str__(self):
        return "%s %s (%s)" % (self.user.first_name, self.user.last_name,
                               self.user.username)

    def save(self, *args, **kwargs):
        self.hashkey = hashlib.sha256(
            (SECRET_KEY + self.user.username).encode('utf-8')).hexdigest()
        super(Owner, self).save(*args, **kwargs)

    def is_manager(self):
        group_ids = self.user.groups.all().values_list('id', flat=True)
        return (
            self.user.is_staff
            and Permission.objects.filter(group__id__in=group_ids).count() > 0)

    @property
    def email(self):
        return self.user.email


@receiver(post_save, sender=User)
def create_owner_profile(sender, instance, created, **kwargs):
    if created:
        try:
            Owner.objects.create(user=instance)
        except Exception as e:
            msg = u'\n Create owner profile ***** Error:%r' % e
            msg += '\n%s' % traceback.format_exc()
            logger.error(msg)
            print(msg)
