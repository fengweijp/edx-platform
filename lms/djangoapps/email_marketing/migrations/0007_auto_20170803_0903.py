# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('email_marketing', '0006_auto_20170711_0615'),
    ]

    operations = [
        migrations.RenameField(
            model_name='emailmarketingconfiguration',
            old_name='sailthru_activation_template',
            new_name='sailthru_welcome_template'
        ),
        migrations.AlterField(
            model_name='emailmarketingconfiguration',
            name='sailthru_welcome_template',
            field=models.CharField(help_text='Sailthru template to use on welcome send. ', max_length=20, blank=True),
        ),
        migrations.AlterField(
            model_name='emailmarketingconfiguration',
            name='welcome_email_send_delay',
            field=models.IntegerField(default=600, help_text='Number of seconds to delay the sending of User Welcome email after user has been created'),
        ),
    ]
