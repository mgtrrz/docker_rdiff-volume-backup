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


def is_true(value):
    """Parse a loosely-formatted boolean-ish string ('true', 'True', ' TRUE ') as True."""
    return str(value).strip().lower() == 'true'


DRY_RUN = is_true(env['DRY_RUN'])

# Setup logging
logging.basicConfig(format='%(asctime)s %(levelname)s: %(message)s', 
                    level=env['LOG_LEVEL'].upper(), 
                    datefmt='%Y-%m-%d %H:%M:%S')

logging.info('Starting Docker volume backups...')

# Initialize Docker client
dclient = docker.from_env()

# Collect environment variables
try:
    volume_driver = env['VOLUME_DRIVER']
    backup_retention = env['BACKUP_RETENTION']
    backups_dir = Path(env['BACKUP_DIR'])
    host_dir = Path(env['HOST_DIR'])
    ignore_cifs = is_true(env['IGNORE_CIFS'])
    force_backup_flag = '--force' if is_true(env['FORCE_BACKUP_CLEANUP']) else ''
except KeyError as e:
    logging.critical('Missing environment variable: %s', str(e))
    exit(1)

if DRY_RUN:
    logging.warning('DRY_RUN is enabled: no containers will be stopped and no backups will be created.')


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
    container_labels = labels(container)
    raw = container_labels.get(RDIFF_LABEL_NAMESPACE + '.exclude-volumes') or ''
    project = container_labels.get('com.docker.compose.project')
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


def backup_and_cleanup_volume(volume):
    """Run rdiff-backup and its retention cleanup for a single volume."""
    volume_mountpoint = host_dir / volume.attrs['Mountpoint'].lstrip('/')
    volume_backup_dir = backups_dir / volume.name

    try:
        result = run(
            ['rdiff-backup', str(volume_mountpoint), str(volume_backup_dir)],
            capture_output=True)
        if result.returncode != 0:
            raise CalledProcessError(result.returncode, result.args,
                                     output=result.stdout, stderr=result.stderr)
        if result.stderr:
            logging.warning(result.stderr.decode('utf-8', 'replace').strip())
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
        if result.returncode != 0 and not looks_like_nothing_to_remove(combined):
            raise CalledProcessError(result.returncode, result.args,
                                     output=result.stdout, stderr=result.stderr)
        if combined and not looks_like_nothing_to_remove(combined):
            logging.warning(combined.decode('utf-8', 'replace').strip())
        logging.info('Successfully cleaned up old backups for volume %s', volume.name)
    except CalledProcessError as e:
        logging.error('Something went wrong cleaning up backups for volume %s: %s',
                      volume.name, (e.stderr or e.output or b'').decode('utf-8', 'replace'))


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
        if label_key.startswith(RDIFF_LABEL_NAMESPACE + '.backup') and is_true(label_value):
            has_backup_label = True
            break
    if has_backup_label:
        labeled_containers.append(container)

if not labeled_containers:
    logging.info('No containers with a %s label found; nothing to do.', RDIFF_LABEL_NAMESPACE + '.backup')
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

# Identify every running container that attaches a volume we would back up.
backup_volume_names = {v.name for v in backup_volumes}
containers_affected = []
for c in dclient.containers.list():
    for mount in (c.attrs.get('Mounts') or []):
        if mount.get('Type') == 'volume' and mount.get('Name') in backup_volume_names:
            containers_affected.append(c)
            break

# Only stop the ones that explicitly opt in via the stop-during-backup` label 
# which defaults to false / not stopped.
containers_to_stop = [
    c for c in containers_affected
    if is_true(labels(c).get(RDIFF_LABEL_NAMESPACE + '.stop-during-backup', 'false'))
]
stop_ids = {c.id for c in containers_to_stop}

if DRY_RUN:
    stop_names = sorted(c.attrs.get('Name').lstrip('/') for c in containers_to_stop)
    logging.warning('DRY_RUN complete. Selected volumes: %s', ', '.join(sorted(backup_volume_names)))
    logging.warning('DRY_RUN would stop/restart these containers: %s', ', '.join(stop_names) or 'none')
    logging.info('Done backing up Docker volumes')
    exit(0)

# Make sure backups directory exists
if not backups_dir.is_dir():
    logging.critical('Backup directory (%s) does not exist.', str(backups_dir))
    exit(1)

# Make sure the `HOST_DIR` exists
if not host_dir.is_dir():
    logging.critical('HOST_DIR (%s) does not exist.', str(host_dir))
    exit(1)

# Map each opted-in container to the specific backup volume(s) it mounts,
# then invert that into volume => containers.
container_backup_volumes = {}   # container.id = set of volume names it mounts
for c in containers_to_stop:
    container_backup_volumes[c.id] = {
        m.get('Name') for m in (c.attrs.get('Mounts') or [])
        if m.get('Type') == 'volume' and m.get('Name') in backup_volume_names
    }

volume_stop_containers = {}   # volume name = containers to stop before backing it up
for c in containers_to_stop:
    for vol_name in container_backup_volumes[c.id]:
        volume_stop_containers.setdefault(vol_name, []).append(c)

containers_by_id = {c.id: c for c in containers_to_stop}

processed_volume_names = set()
for volume in backup_volumes:
    if volume.name in processed_volume_names:
        continue

    if volume.name not in volume_stop_containers:
        processed_volume_names.add(volume.name)
        backup_and_cleanup_volume(volume)
        continue

    unit_containers = {}
    unit_volume_names = set()
    queue = [('volume', volume.name)]
    while queue:
        kind, key = queue.pop()
        if kind == 'volume':
            if key in unit_volume_names:
                continue
            unit_volume_names.add(key)
            for c in volume_stop_containers.get(key, []):
                queue.append(('container', c.id))
        else:
            if key in unit_containers:
                continue
            unit_containers[key] = containers_by_id[key]
            for vol_name in container_backup_volumes[key]:
                queue.append(('volume', vol_name))

    processed_volume_names.update(unit_volume_names)
    unit_volumes = [v for v in backup_volumes if v.name in unit_volume_names]

    stopped_for_unit = []
    try:
        for c in unit_containers.values():
            name = c.attrs.get('Name').lstrip('/')
            logging.info('Stopping container %s before backing up its volume(s)...', name)
            c.stop()
            stopped_for_unit.append(c)
            logging.info('Stopped container %s', name)

        for v in unit_volumes:
            backup_and_cleanup_volume(v)
    finally:
        # Always bring this unit's containers back up, even if a backup raised.
        for c in stopped_for_unit:
            name = c.attrs.get('Name').lstrip('/')
            logging.info('Restarting container %s after backup...', name)
            c.start()
            logging.info('Restarted container %s', name)

# All done
logging.info('Done backing up Docker volumes')
