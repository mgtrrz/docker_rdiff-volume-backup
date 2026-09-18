#!/usr/local/bin/python
import logging
from os import environ as env
from os import getenv

from pathlib import Path
from subprocess import run
from subprocess import CalledProcessError

import docker
from requests.exceptions import ConnectionError

RDIFF_LABEL_NAMESPACE = "com.rdiff-volume-backup"
DRY_RUN = str(getenv('DRY_RUN', False)).strip().lower() == 'true'

# Setup logging
logging.basicConfig(format='%(levelname)s: %(message)s', level=logging.INFO)

logging.info('Starting Docker volume backups...')

# Initialize Docker client
dclient = docker.from_env()

# Collect environment variables
try:
    volume_driver = env['VOLUME_DRIVER']
    backup_retention = env['BACKUP_RETENTION']
    backups_dir = Path(env['BACKUP_DIR'])
    host_dir = Path(env['HOST_DIR'])
    ignore_cifs = env['IGNORE_CIFS'].strip().lower() == 'true'
    force_backup_flag = (
        '--force' if env['FORCE_BACKUP_CLEANUP'] == 'true' else '')
except KeyError as e:
    logging.critical('Missing environment variable: %s', str(e))
    exit(1)

if DRY_RUN:
    logging.warning('DRY_RUN is enabled: no containers will be stopped and no backups will be written.')


def labels(container):
    """Return the labels dict of a container (never None)."""
    config = container.attrs.get('Config', {}) or {}
    return config.get('Labels') or {}


def excluded_volume_names(container):
    """
    Resolve the container's `exclude-volumes` label to a set of real Docker
    volume names.

    Compose names a top-level volume `<project>_<ref>` unless the volume is
    declared `external: true` (in which case it keeps its name verbatim). So
    for a compose-managed container we match both the value as-is and the
    compose-prefixed form; for a raw/swarm container there is no project
    prefix, so only the value as-is is used.
    """
    raw = labels(container).get(RDIFF_LABEL_NAMESPACE + '.exclude-volumes') or ''
    project = labels(container).get('com.docker.compose.project')
    names = set()
    for value in (item.strip() for item in raw.split(',')):
        if not value:
            continue
        names.add(value)
        if project:
            names.add(f'{project}_{value}')
    return names


def is_cifs(volume):
    options = volume.attrs.get('Options') or {}
    return str(options.get('type', '')).lower() == 'cifs'


def looks_like_nothing_to_remove(output):
    """
    rdiff-backup exits non-zero from `--remove-older-than` when there's
    no old increment to delete (which is normal right after a fresh
    backup). That's not worth reporting, so treat it as success.
    """
    if not output:
        return False
    text = output if isinstance(output, str) else output.decode('utf-8', 'replace')
    return 'no increments older than' in text.lower() or \
           'nothing to remove' in text.lower()


# Collect containers and volumes once for caching
try:
    all_containers = dclient.containers.list(all=True)
    all_volumes = {v.name: v for v in dclient.volumes.list()}
except ConnectionError as e:
    logging.critical('Could not connect to the Docker daemon:')
    logging.critical(e)
    exit(1)

# Containers that have opted in to backups via a label under RDIFF_LABEL_NAMESPACE
labeled_containers = []
for container in all_containers:
    has_backup_label = False
    for label_key, label_value in labels(container).items():
        if label_key.startswith(RDIFF_LABEL_NAMESPACE + '.backup') and label_value == "true":
            has_backup_label = True
            break
    if has_backup_label:
        labeled_containers.append(container)

if not labeled_containers:
    logging.info('No containers with a %s label found; nothing to do.', RDIFF_LABEL_NAMESPACE)
    logging.info('Done backing up Docker volumes')
    exit(0)

# Build the set of volumes to back up and track the containers we may stop
excluded = set()
for c in labeled_containers:
    excluded |= excluded_volume_names(c)

backup_volumes = []   # list of volume objects, in stable order
seen = set()
for c in labeled_containers:
    for mount in (c.attrs.get('Mounts') or []):
        if mount.get('Type') != 'volume':
            continue
        volume = all_volumes.get(mount.get('Name'))
        if volume is None:
            logging.warning('Container %s references unknown volume %s; skipping.',
                            c.attrs.get('Name').lstrip('/'), mount.get('Name'))
            continue
        if volume.name in seen:
            continue
        if volume.name in excluded:
            logging.info('  - %s: skipped (excluded)', volume.name)
            continue
        if volume.attrs.get('Driver') != volume_driver:
            logging.info('  - %s: skipped (driver %s != %s)',
                         volume.name, volume.attrs.get('Driver'), volume_driver)
            continue
        if ignore_cifs and is_cifs(volume):
            logging.info('  - %s: skipped (CIFS, IGNORE_CIFS=true)', volume.name)
            continue
        backup_volumes.append(volume)
        seen.add(volume.name)

logging.info('Volumes selected for backup (%d):', len(backup_volumes))
for v in backup_volumes:
    logging.info('    + %s', v.name)

if not backup_volumes:
    logging.warning('No volumes were selected for backup; nothing to do.')
    logging.info('Done backing up Docker volumes')
    exit(0)

# Identify every running container that attaches a volume we would back up (read-only).
backup_volume_names = {v.name for v in backup_volumes}
containers_affected = []
for c in dclient.containers.list():
    for mount in (c.attrs.get('Mounts') or []):
        if mount.get('Type') == 'volume' and mount.get('Name') in backup_volume_names:
            containers_affected.append(c)
            break

if DRY_RUN:
    logging.warning('DRY_RUN complete. Selected volumes: %s', ', '.join(sorted(backup_volume_names)))
    affected_names = sorted(c.attrs.get('Name').lstrip('/') for c in containers_affected)
    logging.warning('DRY_RUN would stop/restart these running containers: %s',
                    ', '.join(affected_names) or 'none')
    logging.info('Done backing up Docker volumes')
    exit(0)

# Make sure backups directory exists
if backups_dir.exists() is False or backups_dir.is_dir() is False:
    logging.critical('Backup directory (%s) does not exist.', str(backups_dir))
    exit(1)

# Make sure the `HOST_DIR` exists
if host_dir.exists() is False or host_dir.is_dir() is False:
    logging.critical('HOST_DIR (%s) does not exist.', str(host_dir))
    exit(1)

# We must stop these before the backup and start them again in the `finally` block so a
# failed backup never leaves a service down. `containers_affected` (computed above) 
# is the set of every running container that mounts a selected volume.
containers_to_stop = containers_affected

stopped = []
try:
    for c in containers_to_stop:
        name = c.attrs.get('Name').lstrip('/')
        logging.info('Stopping container %s before backup...', name)
        c.stop()
        stopped.append(c)
        logging.info('Stopped container %s', name)

    # Make backups
    for volume in backup_volumes:
        volume_mountpoint = host_dir / volume.attrs['Mountpoint'].lstrip('/')
        volume_backup_dir = backups_dir / volume.name

        try:
            result = run(
                ['rdiff-backup', str(volume_mountpoint), str(volume_backup_dir)],
                capture_output=True)
            if result.stderr:
                logging.warning(result.stderr.decode('utf-8', 'replace').strip())
            if result.returncode != 0:
                raise CalledProcessError(result.returncode, result.args,
                                         output=result.stdout, stderr=result.stderr)
            logging.info('Successfully backed up volume %s', volume.name)
        except CalledProcessError as e:
            logging.error('Something went wrong running backup for volume %s: %s',
                          volume.name, (e.stderr or e.output or b'').decode('utf-8', 'replace'))

        # Clean up backups older than the `BACKUP_RETENTION`. rdiff-backup exits non-zero when there is 
        # nothing old to remove.
        try:
            cleanup_args = (['rdiff-backup', '--remove-older-than', backup_retention]
                            + ([force_backup_flag] if force_backup_flag else [])
                            + [str(volume_backup_dir)])
            result = run(cleanup_args, capture_output=True)
            combined = (result.stdout or b'') + (result.stderr or b'')
            if combined and not looks_like_nothing_to_remove(combined):
                logging.warning(combined.decode('utf-8', 'replace').strip())
            if result.returncode != 0 and not looks_like_nothing_to_remove(combined):
                raise CalledProcessError(result.returncode, result.args,
                                         output=result.stdout, stderr=result.stderr)
            logging.info('Successfully cleaned up old backups for volume %s', volume.name)
        except CalledProcessError as e:
            logging.error('Something went wrong cleaning up backups for volume %s: %s',
                          volume.name, (e.stderr or e.output or b'').decode('utf-8', 'replace'))
finally:
    # Always bring the containers back up, even if the backup raised.
    for c in stopped:
        name = c.attrs.get('Name').lstrip('/')
        logging.info('Restarting container %s after backup...', name)
        c.start()
        logging.info('Restarted container %s', name)

# All done
logging.info('Done backing up Docker volumes')
