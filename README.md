# Docker rdiff-volume-backup

This is a Docker image that is meant to be used to backup your Docker volumes using the [rdiff-backup](http://rdiff-backup.nongnu.org/) tool for incremental backups. If you want to backup a host directory instead of Docker volumes, [tozd/rdiff-backup](https://hub.docker.com/r/tozd/rdiff-backup/) would be a more suitable container.

This is a fork of kadimasolutions' [docker-rdiff-volume-backup](https://github.com/kadimasolutions/docker_rdiff-volume-backup) and covers some specific nuances about my Docker setup, particularly with CIFS mounted docker volumes. This script also allows stopping the container before backing it up and restarting it to handle sensitive applications, such as databases.

Finally, instead of backing up every volume in Docker, you can specify which volume to backup with labels.


## Usage

### Summary

This container needs to mount the Docker socket to inspect containers and their volumes, and also to stop/restart containers during a backup. The directory containing the volumes for the `VOLUME_DRIVER` ( `local` by default ) also needs to be mounted. Finally, you need to bind mount a host directory or a Docker volume ( with a different driver than the volumes that you want to back up ) to the `/backup` directory to persist the backups.

**Which volumes get backed up is driven by labels that you add to the container.** A volume is included only if *all* of the following are true:

- it is mounted by a container that has at least one label: `com.rdiff-volume-backup.backup: true`,
- its `Driver` matches `VOLUME_DRIVER` ('local' by default),
- it is **not** a CIFS mount (when `IGNORE_CIFS=true`, the default), and
- it is **not** listed in that container's `com.rdiff-volume-backup.exclude-volumes` label.

By default, containers are not stopped when backups of its volumes are taken. When the `stop-during-backup` label is set to true, the container is stopped before rdiff backups begin. See the below section for more information on these labels.

Backups will run on the given `CRON_SCHEDULE` which is `0 0 * * *` ( daily at 12:00am ) by default. rdiff-backup will keep diffs that allow you to reproduce any backup up to the `BACKUP_RETENTION` time period which is `12M` ( 12 months ) by default. Each volume is backed up individually using rdiff-backup to a directory in `/backup` of the same name.

A Docker Compose file would look like this:

**docker-compose.yml**
```yml
services:
  rdiff-volume-backup:
    image: kadimasolutions/rdiff-volume-backup
    volumes:
     - /var/run/docker.sock:/var/run/docker.sock:ro
     # This will be different for different volume drivers and Docker
     # configurations
     - /var/lib/docker/volumes:/host/var/lib/docker/volumes:ro
     # Bind mount a host directory to persist backups OR
     - /backup:/backup
     # Mount a docker volume with a different driver
     #- volume-backups:/backup
    environment:
      VOLUME_DRIVER: local
      CRON_SCHEDULE: 0 0 * * *
      BACKUP_RETENTION: 12M

# Uncomment if using Docker volume to persist backups
#volumes:
  #volume-backups:
    #driver: docker-volume-driver-that-is-not-the-same-as-VOLUME_DRIVER
  
# Backing up to a NAS? Consider a CIFS mount
#volumes:
  #volume-backups:
    #driver: local
    #driver_opts:
      #type: cifs
      #device: //<nas-ip-address>/Backups
      #o: "username=smbuser,password=${BACKUP_PW},uid=1000,gid=1000,file_mode=0664,dir_mode=0775"
```

### Identifying the Docker Volume mountpoint

In order for the container to have access to the Docker volumes, the directory containing the Docker volume mountpoints needs to be mounted into the container. This directory can be different depending on your Docker configuration and the volume driver. To find out the volume directory for your particular config and driver first run `docker volume ls` to get the list of docker volume on your host.

```
$ docker volume ls
DRIVER              VOLUME NAME
local               4d3b80341630e9f114e845ccbbe483b0b24595c31d2feeac37ec2df4719a83c2
local               795cd38166cd483f305ab27056d82da20efcfc4a0170295c6573bc76b5e17dae
rancher-nfs         ad520ada61447252479e5f1e0a6f4a3c478ab6d06e9ca218c54992c192e99d09
```

Next pick a volume with the driver that you want to backup and run `docker volume inspect <volume-name>`.

```
$ docker volume inspect 4d3b80341630e9f114e845ccbbe483b0b24595c31d2feeac37ec2df4719a83c2
[
    {
        "Driver": "local",
        "Labels": null,
        "Mountpoint": "/mnt/sda1/var/lib/docker/volumes/4d3b80341630e9f114e845ccbbe483b0b24595c31d2feeac37ec2df4719a83c2/_data",
        "Name": "4d3b80341630e9f114e845ccbbe483b0b24595c31d2feeac37ec2df4719a83c2",
        "Options": {},
        "Scope": "local"
    }
]
```

We are specifically interested in the `Mountpoint` key. The directory that we need to mount into the container is the directory that contains all of the docker volumes of this volume driver. In this case that is `/mnt/sda1/var/lib/docker/volumes/`. This directory should be mounted into the container with the `/host` prefix: `/host/mnt/sda1/var/lib/docker/volumes/`.

### Environment Variables

The full list of environment variables.

#### VOLUME_DRIVER

The `VOLUME_DRIVER` restricts backups to volumes whose driver matches this value. It is applied *on top of* the container-label selection: a volume is only backed up if its driver equals `VOLUME_DRIVER` (see the bullet list in the Summary above). Most local, NFS-backed and CIFS/SMB "bind" volumes report `local` as their driver, so this filter alone does not exclude your NAS shares — use `IGNORE_CIFS` and `exclude-volumes` for that.

**Default:** `local`

#### CRON_SCHEDULE

The `CRON_SCHEDULE` is the schedule on which the backup script will be executed.

**Default:** `0 0 * * *` ( Run daily at 12:00am )

#### BACKUP_RETENTION

The `BACKUP_RETENTION` is the length of time that the backup history is kept. Any backups older than the specified time will be deleted when a new backup is made. The details of the time format can be found on the [rdiff-backup man page](https://github.com/sol1/rdiff-backup/blob/8ccc5a3b44c996ecd810f8d5d586d0da6435cc32/rdiff-backup/rdiff-backup.1#L601).

**Default:** `12M` ( 12 months )
**See also:** `FORCE_BACKUP_CLEANUP`

#### FORCE_BACKUP_CLEANUP

If `FORCE_BACKUP_CLEANUP` is set to 'true' the container will clean up backups older than the `BACKUP_RETENTION` even if it means deleting multiple revisions. It is slightly safer to leave this set to `false` because it will be sure not to delete more than one old revision at a time. In case of a mistake in the value of `BACKUP_RETENTION`, it will preven the deletion of a large portion of backup history.

**Default:** `false`

#### BACKUP_DIR

The directory in the container to make backups to. In order for the backups to be useful, you must either bind mount a host directory or mount a docker volume ( with a different driver than the one that you are backup up ) to this path in order to persist the backups.

**Default:** `/backup`

#### HOST_DIR

The `HOST_DIR` is the prefix that should be applied to the Docker volume path when mounting the Docker volumes directory. This shouldn't need to be changed for any reason.

**Default:** `/host`

#### RUN_BACKUP_ON_START

Start a backup as soon as the container starts. 

**Default:** `false`

#### IGNORE_CIFS

When set to `true`, volumes whose `driver_opts` declare `type: cifs` are skipped, even though Docker reports their `Driver` as `local`. This is how Docker models a NFS/SMB bind-mount through a "local" volume — the data already lives on a remote share, so there is nothing to back up locally.

**Default:** `true`

#### DRY_RUN

When set to `true`, the script runs only the volume-selection pass and prints the volumes it *would* back up plus the running containers it *would* stop, then exits without stopping anything and without writing any backups. Useful for verifying your labels before the first real run.

**Default:** `false`

#### LOG_LEVEL

Logging level to display. From most verbose to least: debug, info, warning, error, critical.

**Default:** `info`

### Container Labels

A container is only considered for backups if it carries the `com.rdiff-volume-backup.backup` label. Two additional keys can be used:

| Label | Default | Meaning |
|---|---|---|
| `com.rdiff-volume-backup.backup` | *(unset)* | _Required_ . Must be set for the script to work and recognizing which containers' volumes to backup _and_ must be set to true to enable backups. Can be disabled with false. |
| `com.rdiff-volume-backup.stop-during-backup` | `false` | _Optional_. Whether to stop containers before backing up its volumes. False is the default behavior if this is omitted. Set to true for sensitive applications such as databases. |
| `com.rdiff-volume-backup.exclude-volumes` | *(unset)* | _Optional_. Comma-separated list of volume names to skip for this container. Values may use the compose short name (e.g. `media`) and the script resolves them to the real Docker name (`mycomposeproj_media`). External / `external:true` volumes should be written with their full name. |

Example:

```yml
services:
  mycomposeproj:
    image: example/app:latest
    labels:
      com.rdiff-volume-backup.backup: "true"
      com.rdiff-volume-backup.stop-during-backup: "true"  # Optional, defaults to false if omitted.
      com.rdiff-volume-backup.exclude-volumes: "media"    # Optional, backs up all volumes if omitted.
    volumes:
      - config:/myapp/config   # Would be backed up
      - media:/media           # This one would be skipped

volumes:
  config:
  media:
    external: true
```

## Known issues

I would have liked for this to look at labels on volumes instead to determine which volume to backup, but annoyingly, volume objects in Docker are immutable once created. Therefore, I did not find it practical to delete volumes (and their contents) just to add/modify/remove a new label just to specify it.
