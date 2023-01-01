"""Global user state, backed by a sqlite database.

Concepts:
- Cluster name: a user-supplied or auto-generated unique name to identify a
  cluster.
- Cluster handle: (non-user facing) an opaque backend handle for us to
  interact with a cluster.
"""
import enum
import json
import os
import pathlib
import pickle
import time
import typing
from typing import Any, Dict, List, Optional

from sky import clouds
from sky.utils import db_utils
from sky.utils import common_utils

if typing.TYPE_CHECKING:
    from sky import backends
    from sky.data import Storage

_ENABLED_CLOUDS_KEY = 'enabled_clouds'

_DB_PATH = os.path.expanduser('~/.sky/state.db')
pathlib.Path(_DB_PATH).parents[0].mkdir(parents=True, exist_ok=True)


def create_table(cursor, conn):
    # Enable WAL mode to avoid locking issues.
    # See: issue #1441 and PR #1509
    conn.execute('PRAGMA journal_mode=WAL')
    # Table for Clusters
    cursor.execute("""\
        CREATE TABLE IF NOT EXISTS clusters (
        name TEXT PRIMARY KEY,
        launched_at INTEGER,
        handle BLOB,
        last_use TEXT,
        status TEXT,
        autostop INTEGER DEFAULT -1)""")
    # Table for configs (e.g. enabled clouds)
    cursor.execute("""\
        CREATE TABLE IF NOT EXISTS config (
        key TEXT PRIMARY KEY, value TEXT)""")
    # Table for Storage
    cursor.execute("""\
        CREATE TABLE IF NOT EXISTS storage (
        name TEXT PRIMARY KEY,
        launched_at INTEGER,
        handle BLOB,
        last_use TEXT,
        status TEXT)""")
    # For backward compatibility.
    # TODO(zhwu): Remove this function after all users have migrated to
    # the latest version of SkyPilot.
    # Add autostop column to clusters table
    db_utils.add_column_to_table(cursor, conn, 'clusters', 'autostop',
                                 'INTEGER DEFAULT -1')

    db_utils.add_column_to_table(cursor, conn, 'clusters', 'metadata',
                                 'TEXT DEFAULT "{}"')

    db_utils.add_column_to_table(cursor, conn, 'clusters', 'to_down',
                                 'INTEGER DEFAULT 0')
    db_utils.add_column_to_table(cursor, conn, 'clusters', 'owner', 'TEXT')
    conn.commit()


_DB = db_utils.SQLiteConn(_DB_PATH, create_table)


class ClusterStatus(enum.Enum):
    """Cluster status as recorded in table 'clusters'."""
    # NOTE: these statuses are as recorded in our local cache, the table
    # 'clusters'.  The actual cluster state may be different (e.g., an UP
    # cluster getting killed manually by the user or the cloud provider).

    # Initializing.  This means a backend.provision() call has started but has
    # not successfully finished. The cluster may be undergoing setup, may have
    # failed setup, may be live or down.
    INIT = 'INIT'

    # The cluster is recorded as up.  This means a backend.provision() has
    # previously succeeded.
    UP = 'UP'

    # Stopped.  This means a `sky stop` call has previously succeeded.
    STOPPED = 'STOPPED'


class StorageStatus(enum.Enum):
    """Storage status as recorded in table 'storage'."""

    # Initializing and uploading storage
    INIT = 'INIT'

    # Initialization failed
    INIT_FAILED = 'INIT_FAILED'

    # Failed to Upload to Cloud
    UPLOAD_FAILED = 'UPLOAD_FAILED'

    # Finished uploading, in terminal state
    READY = 'READY'


def add_or_update_cluster(cluster_name: str,
                          cluster_handle: 'backends.Backend.ResourceHandle',
                          ready: bool,
                          is_launch: bool = True):
    """Adds or updates cluster_name -> cluster_handle mapping.

    Args:
        cluster_name: Name of the cluster.
        cluster_handle: Backend.ResourceHandle of the cluster.
        ready: Whether the cluster is ready to use. If False, the cluster will
            be marked as INIT, otherwise it will be marked as UP.
        is_launch: if the cluster is firstly launched. If True, the launched_at
            and last_use will be updated. Otherwise, use the old value.
    """
    # FIXME: launched_at will be changed when `sky launch -c` is called.
    handle = pickle.dumps(cluster_handle)
    cluster_launched_at = int(time.time()) if is_launch else None
    last_use = common_utils.get_pretty_entry_point() if is_launch else None
    status = ClusterStatus.UP if ready else ClusterStatus.INIT
    _DB.cursor.execute(
        'INSERT or REPLACE INTO clusters'
        # All the fields need to exist here, even if they don't need
        # be changed, as the INSERT OR REPLACE statement will replace
        # the field of the existing row with the default value if not
        # specified.
        '(name, launched_at, handle, last_use, status, '
        'autostop, to_down, metadata, owner) '
        'VALUES ('
        # name
        '?, '
        # launched_at
        'COALESCE('
        '?, (SELECT launched_at FROM clusters WHERE name=?)), '
        # handle
        '?, '
        # last_use
        'COALESCE('
        '?, (SELECT last_use FROM clusters WHERE name=?)), '
        # status
        '?, '
        # autostop
        # Keep the old autostop value if it exists, otherwise set it to
        # default -1.
        'COALESCE('
        '(SELECT autostop FROM clusters WHERE name=? AND status!=?), -1), '
        # Keep the old to_down value if it exists, otherwise set it to
        # default 0.
        'COALESCE('
        '(SELECT to_down FROM clusters WHERE name=? AND status!=?), 0), '
        # Keep the old metadata value if it exists, otherwise set it to
        # default {}.
        'COALESCE('
        '(SELECT metadata FROM clusters WHERE name=?), "{}"),'
        # Keep the old owner value if it exists, otherwise set it to
        # default null.
        'COALESCE('
        '(SELECT owner FROM clusters WHERE name=?), null)'
        ')',
        (
            # name
            cluster_name,
            # launched_at
            cluster_launched_at,
            cluster_name,
            # handle
            handle,
            # last_use
            last_use,
            cluster_name,
            # status
            status.value,
            # autostop
            cluster_name,
            ClusterStatus.STOPPED.value,
            # to_down
            cluster_name,
            ClusterStatus.STOPPED.value,
            # metadata
            cluster_name,
            # owner
            cluster_name,
        ))
    _DB.conn.commit()


def update_last_use(cluster_name: str):
    """Updates the last used command for the cluster."""
    _DB.cursor.execute('UPDATE clusters SET last_use=(?) WHERE name=(?)',
                       (common_utils.get_pretty_entry_point(), cluster_name))
    _DB.conn.commit()


def remove_cluster(cluster_name: str, terminate: bool):
    """Removes cluster_name mapping."""
    if terminate:
        _DB.cursor.execute('DELETE FROM clusters WHERE name=(?)',
                           (cluster_name,))
    else:
        handle = get_handle_from_cluster_name(cluster_name)
        if handle is None:
            return
        if hasattr(handle, 'stable_internal_external_ips'):
            # Must invalidate IP list: otherwise 'sky cpunode'
            # on a stopped cpunode will directly try to ssh, which leads to timeout.
            handle.stable_internal_external_ips = None
            _DB.cursor.execute(
                'UPDATE clusters SET handle=(?), status=(?) '
                'WHERE name=(?)', (
                    pickle.dumps(handle),
                    ClusterStatus.STOPPED.value,
                    cluster_name,
                ))
    _DB.conn.commit()


def get_handle_from_cluster_name(
        cluster_name: str) -> Optional['backends.Backend.ResourceHandle']:
    assert cluster_name is not None, 'cluster_name cannot be None'
    rows = _DB.cursor.execute('SELECT handle FROM clusters WHERE name=(?)',
                              (cluster_name,))
    for (handle,) in rows:
        return pickle.loads(handle)
    return None


def get_glob_cluster_names(cluster_name: str) -> List[str]:
    assert cluster_name is not None, 'cluster_name cannot be None'
    rows = _DB.cursor.execute('SELECT name FROM clusters WHERE name GLOB (?)',
                              (cluster_name,))
    return [row[0] for row in rows]


def set_cluster_status(cluster_name: str, status: ClusterStatus) -> None:
    _DB.cursor.execute('UPDATE clusters SET status=(?) WHERE name=(?)', (
        status.value,
        cluster_name,
    ))
    count = _DB.cursor.rowcount
    _DB.conn.commit()
    assert count <= 1, count
    if count == 0:
        raise ValueError(f'Cluster {cluster_name} not found.')


def set_cluster_autostop_value(cluster_name: str, idle_minutes: int,
                               to_down: bool) -> None:
    _DB.cursor.execute(
        'UPDATE clusters SET autostop=(?), to_down=(?) WHERE name=(?)', (
            idle_minutes,
            int(to_down),
            cluster_name,
        ))
    count = _DB.cursor.rowcount
    _DB.conn.commit()
    assert count <= 1, count
    if count == 0:
        raise ValueError(f'Cluster {cluster_name} not found.')


def get_cluster_metadata(cluster_name: str) -> Optional[Dict[str, Any]]:
    rows = _DB.cursor.execute('SELECT metadata FROM clusters WHERE name=(?)',
                              (cluster_name,))
    for (metadata,) in rows:
        if metadata is None:
            return None
        return json.loads(metadata)
    return None


def set_cluster_metadata(cluster_name: str, metadata: Dict[str, Any]) -> None:
    _DB.cursor.execute('UPDATE clusters SET metadata=(?) WHERE name=(?)', (
        json.dumps(metadata),
        cluster_name,
    ))
    count = _DB.cursor.rowcount
    _DB.conn.commit()
    assert count <= 1, count
    if count == 0:
        raise ValueError(f'Cluster {cluster_name} not found.')


def set_owner_identity_for_cluster(cluster_name: str,
                                   owner_identity: Optional[str]) -> None:
    if owner_identity is None:
        return
    _DB.cursor.execute('UPDATE clusters SET owner=(?) WHERE name=(?)',
                       (owner_identity, cluster_name))
    count = _DB.cursor.rowcount
    _DB.conn.commit()
    assert count <= 1, count
    if count == 0:
        raise ValueError(f'Cluster {cluster_name} not found.')


def get_cluster_from_name(
        cluster_name: Optional[str]) -> Optional[Dict[str, Any]]:
    rows = _DB.cursor.execute('SELECT * FROM clusters WHERE name=(?)',
                              (cluster_name,)).fetchall()
    for row in rows:
        # Explicitly specify the number of fields to unpack, so that
        # we can add new fields to the database in the future without
        # breaking the previous code.
        (name, launched_at, handle, last_use, status, autostop, metadata,
         to_down, owner) = row[:9]
        # TODO: use namedtuple instead of dict
        record = {
            'name': name,
            'launched_at': launched_at,
            'handle': pickle.loads(handle),
            'last_use': last_use,
            'status': ClusterStatus[status],
            'autostop': autostop,
            'to_down': bool(to_down),
            'owner': owner,
            'metadata': json.loads(metadata),
        }
        return record


def get_clusters() -> List[Dict[str, Any]]:
    rows = _DB.cursor.execute(
        'select * from clusters order by launched_at desc').fetchall()
    records = []
    for row in rows:
        (name, launched_at, handle, last_use, status, autostop, metadata,
         to_down, owner) = row[:9]
        # TODO: use namedtuple instead of dict
        record = {
            'name': name,
            'launched_at': launched_at,
            'handle': pickle.loads(handle),
            'last_use': last_use,
            'status': ClusterStatus[status],
            'autostop': autostop,
            'to_down': bool(to_down),
            'owner': owner,
            'metadata': json.loads(metadata),
        }
        records.append(record)
    return records


def get_cluster_names_start_with(starts_with: str) -> List[str]:
    rows = _DB.cursor.execute('SELECT name FROM clusters WHERE name LIKE (?)',
                              (f'{starts_with}%',))
    return [row[0] for row in rows]


def get_enabled_clouds() -> List[clouds.Cloud]:
    rows = _DB.cursor.execute('SELECT value FROM config WHERE key = ?',
                              (_ENABLED_CLOUDS_KEY,))
    ret = []
    for (value,) in rows:
        ret = json.loads(value)
        break
    return [clouds.CLOUD_REGISTRY.from_str(cloud) for cloud in ret]


def set_enabled_clouds(enabled_clouds: List[str]) -> None:
    _DB.cursor.execute('INSERT OR REPLACE INTO config VALUES (?, ?)',
                       (_ENABLED_CLOUDS_KEY, json.dumps(enabled_clouds)))
    _DB.conn.commit()


def add_or_update_storage(storage_name: str,
                          storage_handle: 'Storage.StorageMetadata',
                          storage_status: StorageStatus):
    storage_launched_at = int(time.time())
    handle = pickle.dumps(storage_handle)
    last_use = common_utils.get_pretty_entry_point()

    def status_check(status):
        return status in StorageStatus

    if not status_check(storage_status):
        raise ValueError(f'Error in updating global state. Storage Status '
                         f'{storage_status} is passed in incorrectly')
    _DB.cursor.execute('INSERT OR REPLACE INTO storage VALUES (?, ?, ?, ?, ?)',
                       (storage_name, storage_launched_at, handle, last_use,
                        storage_status.value))
    _DB.conn.commit()


def remove_storage(storage_name: str):
    """Removes Storage from Database"""
    _DB.cursor.execute('DELETE FROM storage WHERE name=(?)', (storage_name,))
    _DB.conn.commit()


def set_storage_status(storage_name: str, status: StorageStatus) -> None:
    _DB.cursor.execute('UPDATE storage SET status=(?) WHERE name=(?)', (
        status.value,
        storage_name,
    ))
    count = _DB.cursor.rowcount
    _DB.conn.commit()
    assert count <= 1, count
    if count == 0:
        raise ValueError(f'Storage {storage_name} not found.')


def get_storage_status(storage_name: str) -> None:
    assert storage_name is not None, 'storage_name cannot be None'
    rows = _DB.cursor.execute('SELECT status FROM storage WHERE name=(?)',
                              (storage_name,))
    for (status,) in rows:
        return StorageStatus[status]


def set_storage_handle(storage_name: str, handle: 'Storage.StorageMetadata'):
    _DB.cursor.execute('UPDATE storage SET handle=(?) WHERE name=(?)', (
        pickle.dumps(handle),
        storage_name,
    ))
    count = _DB.cursor.rowcount
    _DB.conn.commit()
    assert count <= 1, count
    if count == 0:
        raise ValueError(f'Storage{storage_name} not found.')


def get_handle_from_storage_name(
        storage_name: Optional[str]) -> Optional['Storage.StorageMetadata']:
    if storage_name is None:
        return None
    rows = _DB.cursor.execute('SELECT handle FROM storage WHERE name=(?)',
                              (storage_name,))
    for (handle,) in rows:
        if handle is None:
            return None
        return pickle.loads(handle)


def get_glob_storage_name(storage_name: str) -> List[str]:
    assert storage_name is not None, 'storage_name cannot be None'
    rows = _DB.cursor.execute('SELECT name FROM storage WHERE name GLOB (?)',
                              (storage_name,))
    return [row[0] for row in rows]


def get_storage_names_start_with(starts_with: str) -> List[str]:
    rows = _DB.cursor.execute('SELECT name FROM storage WHERE name LIKE (?)',
                              (f'{starts_with}%',))
    return [row[0] for row in rows]


def get_storage() -> List[Dict[str, Any]]:
    rows = _DB.cursor.execute('select * from storage')
    records = []
    for name, launched_at, handle, last_use, status in rows:
        # TODO: use namedtuple instead of dict
        records.append({
            'name': name,
            'launched_at': launched_at,
            'handle': pickle.loads(handle),
            'last_use': last_use,
            'status': StorageStatus[status],
        })
    return records
