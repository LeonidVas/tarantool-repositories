#!/bin/bash
set -eu


help() {
	cat <<EOF
rws_backup.sh - util for working with S3 backups.

Usage:
	rws_backup.sh COMMAND

Commands:
	help        Show help.
	backup      Create a backup.

To specify parameters the environment variables are used.

Parameters:
	AWSACCESSKEYID      Access Key ID for the account.
	AWSSECRETACCESSKEY  Secret Access Key for the account.
	RESTIC_PASSWORD     Restic repository password.
	RWS_BACKUP_URL      URL to use to access S3 (example: https://hb.bizmrg.com).
	RWS_BACKUP_BUCKET   Backed up bucket name.
	RWS_BACKUP_PATH     Path inside the backed up bucket (if want to back up
	                    only a part of the bucket).
	RWS_BACKUP_DEST     Path to save the backup (examples: /path/to/backup ,
	                    s3:hb.bizmrg.com/backups-bucket/backup). When using S3
	                    as the backup destination, the same AWS Access key will
	                    be used for both the backup source bucket and the backup
	                    destination bucket.
EOF
}


cleanup() {
	# Cleanup after complete backup.

	# We use "set -e" but we don't want to exit if the "unmount"
	# command fails, because a temporary directory may has been
	# created and some error occurred while trying to mount.
	# Therefore, we use "|| true".
	umount "${S3_MOUNT_POINT}" || true
	rmdir "${S3_MOUNT_POINT}"
}


prepare_restic_repo() {
	# Check if the repository exists
	# (see https://github.com/restic/restic/issues/1690).
	# Init the repository if it doesn't initialized or exit if the
	# password is wrong.
	local CHECK_RESULT=0
	local NOT_INIT_ERROR="Fatal: unable to open config file:"
	CHECK_OUT=$(restic --repo "${RWS_BACKUP_DEST}" cat config 2>&1) \
		|| CHECK_RESULT=${?}

	if [[ ${CHECK_RESULT} != 0 ]]; then
		if echo "${CHECK_OUT}" | grep -q "${NOT_INIT_ERROR}" ; then
			restic init --repo "${RWS_BACKUP_DEST}"
		else
			echo "${CHECK_OUT}" 1>&2
			exit 1
		fi
	fi
}


backup() {
	# Backs up the specified suite / repo / whatever from S3.

	# Create a mount point for the backed up bucket.
	S3_MOUNT_POINT=$(mktemp --directory .s3fs_mountpoint_XXXXX)
	# Set a handler on failure.
	trap cleanup EXIT

	prepare_restic_repo

	# Mount the specified bucket and create a backup using "proot".
	# "proot" is needed to remove the mount point prefix on one side
	# and for restic to treat the files as the same regardless of the
	# s3fs mount point on the other side
	# (see https://github.com/restic/restic/issues/2092).
	s3fs ${RWS_BACKUP_BUCKET} ${S3_MOUNT_POINT} -o url=${RWS_BACKUP_URL}
	# (cd ${S3_MOUNT_POINT}/${RWS_BACKUP_PATH} \
	# 	&& restic --repo ${RWS_BACKUP_DEST} backup .)
	proot -b "${S3_MOUNT_POINT}/${RWS_BACKUP_PATH}":"${RWS_BACKUP_PATH}" \
		restic --repo "${RWS_BACKUP_DEST}" backup "${RWS_BACKUP_PATH}"

	# Cleanup and disable the failure handler.
	cleanup
	trap - EXIT
}


# Choose an action or display help.
for arg in "$@"; do
	case "$arg" in
		"help")
			help;;
		"backup")
			backup;;
		*)
			help
			exit 1;;
	esac
	exit 0
done

help
exit 1
