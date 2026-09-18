#!/bin/sh

# Setup the crontab
echo "$CRON_SCHEDULE /run-backup.py" | crontab -

if [ "$RUN_BACKUP_ON_START" = true ]; then
  ./run-backup.py
fi

# Start the cron daemon
exec crond -f
