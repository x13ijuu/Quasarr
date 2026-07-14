# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

import multiprocessing
import os
import sys
import tempfile
import time

from dotenv import load_dotenv

import quasarr.providers.web_server
from quasarr.api import get_api
from quasarr.constants import FALLBACK_USER_AGENT
from quasarr.providers import shared_state, version
from quasarr.providers.hostname_issues import clear_all_hostname_issues
from quasarr.providers.log import (
    crit,
    debug,
    error,
    get_log_level,
    info,
    log_level_names,
)
from quasarr.providers.notifications import send_notification
from quasarr.providers.notifications.helpers.notification_types import NotificationType
from quasarr.providers.utils import (
    Unbuffered,
    check_flaresolverr,
    check_ip,
    extract_allowed_keys,
    validate_address,
)
from quasarr.search.sources.helpers import (
    get_login_required_hostnames,
    get_radarr_required_hostnames,
    get_sonarr_required_hostnames,
)
from quasarr.storage.config import Config, get_clean_hostnames
from quasarr.storage.setup import (
    flaresolverr_config,
    hostname_credentials_config,
    hostnames_config,
    initialize_filecrypt_setting,
    initialize_notification_settings,
    initialize_radarr_client,
    initialize_sonarr_client,
    initialize_timeout_slow_mode_settings,
    is_radarr_configured,
    is_radarr_skipped,
    is_sonarr_configured,
    is_sonarr_skipped,
    jdownloader_config,
    path_config,
    radarr_config,
    sonarr_config,
)
from quasarr.storage.sqlite_database import DataBase

load_dotenv(override=True)


def run():
    with multiprocessing.Manager() as manager:
        shared_state_dict = manager.dict()
        shared_state_lock = manager.Lock()
        shared_state.set_state(shared_state_dict, shared_state_lock)

        sys.stdout = Unbuffered(sys.stdout)

        print(f"""┌────────────────────────────────────┐
  Quasarr {version.get_fork_version()} by RiX
  https://github.com/rix1337/Quasarr
  fork: https://github.com/x13ijuu/Quasarr
└────────────────────────────────────┘""")

        print("\n===== Recommended Services =====")
        print('👉 Fast premium downloads: "https://linksnappy.com/?ref=397097" 👈')
        print(
            '👉 Automated CAPTCHA solutions: "https://github.com/rix1337/Quasarr?tab=readme-ov-file#sponsorshelper" 👈'
        )

        port = int(os.environ.get("PORT", "8080"))
        internal_address_env = os.environ.get("INTERNAL_ADDRESS")
        external_address_env = os.environ.get("EXTERNAL_ADDRESS")

        config_path = ""
        if os.environ.get("DOCKER"):
            config_path = "/config"
            if not internal_address_env:
                error(
                    "You must set the INTERNAL_ADDRESS variable to a locally reachable URL, e.g. http://192.168.1.1:8080"
                    + " The local URL will be used by Radarr/Sonarr to connect to Quasarr"
                    + " Stopping Quasarr..."
                )
                sys.exit(1)
            internal_address = internal_address_env
        else:
            if internal_address_env:
                internal_address = internal_address_env
            else:
                internal_address = f"http://{check_ip()}:{port}"

        if external_address_env:
            external_address = external_address_env
        else:
            external_address = internal_address

        validate_address(internal_address, "INTERNAL_ADDRESS")
        validate_address(external_address, "EXTERNAL_ADDRESS")

        shared_state.set_connection_info(internal_address, external_address, port)

        if not config_path:
            config_path_file = "Quasarr.conf"
            if not os.path.exists(config_path_file):
                path_config(shared_state)
            with open(config_path_file, "r") as f:
                config_path = f.readline().strip()

        os.makedirs(config_path, exist_ok=True)

        try:
            temp_file = tempfile.TemporaryFile(dir=config_path)
            temp_file.close()
        except Exception as e:
            error(f'Could not access "{config_path}": {e}"Stopping Quasarr...')
            sys.exit(1)

        shared_state.set_files(config_path)
        if DataBase.maintain(shared_state.values["dbfile"]) is False:
            error("Stopping Quasarr because Quasarr.db failed its integrity check.")
            sys.exit(1)
        Config.prune_unsupported_keys(shared_state.values["configfile"])
        shared_state.update("config", Config)
        shared_state.update("database", DataBase)
        supported_hostnames = extract_allowed_keys(Config._DEFAULT_CONFIG, "Hostnames")
        shared_state.update("sites", [key.upper() for key in supported_hostnames])
        # Set fallback user agent immediately so it's available while background check runs
        shared_state.update("user_agent", FALLBACK_USER_AGENT)
        shared_state.update("helper_active", False)

        cleared_hostname_issues = clear_all_hostname_issues()
        if cleared_hostname_issues:
            info(
                f"Cleared {cleared_hostname_issues} stale hostname issue{'s' if cleared_hostname_issues != 1 else ''} on startup"
            )

        hostnames = get_clean_hostnames(shared_state)
        if not hostnames:
            hostnames_config(shared_state)
            hostnames = get_clean_hostnames(shared_state)

        # Check credentials for login-required hostnames
        skip_login_db = DataBase("skip_login")

        quasarr.providers.web_server.temp_server_success = False

        for site in get_login_required_hostnames():
            hostname = Config("Hostnames").get(site)
            if hostname:
                # dj and sj share the same credentials under JUNKIES
                section = "JUNKIES" if site in ["dj", "sj"] else site.upper()
                site_config = Config(section)
                user = site_config.get("user")
                password = site_config.get("password")
                if not user or not password:
                    skip_val = skip_login_db.retrieve(site)
                    if skip_val and str(skip_val).lower() == "true":
                        info(f'"{site.upper()}" login skipped by user preference')
                    else:
                        info(
                            f'"{site.upper()}" credentials missing. Launching setup...'
                        )
                        quasarr.providers.web_server.temp_server_success = False
                        hostname_credentials_config(
                            shared_state, site.upper(), hostname
                        )

        # Check FlareSolverr configuration
        skip_flaresolverr_db = DataBase("skip_flaresolverr")
        flaresolverr_skipped = skip_flaresolverr_db.retrieve("skipped")
        flaresolverr_url = Config("FlareSolverr").get("url")

        if not flaresolverr_url and not flaresolverr_skipped:
            flaresolverr_config(shared_state)

        # Check Radarr configuration if any configured hostname requires it
        initialize_radarr_client(shared_state)
        if not is_radarr_configured(shared_state) and not is_radarr_skipped():
            radarr_required_sites = [
                site
                for site in get_radarr_required_hostnames()
                if Config("Hostnames").get(site)
            ]
            if radarr_required_sites:
                radarr_config(shared_state, radarr_required_sites)
                initialize_radarr_client(shared_state)

        # Check Sonarr configuration if any configured hostname requires it
        initialize_sonarr_client(shared_state)
        if not is_sonarr_configured(shared_state) and not is_sonarr_skipped():
            sonarr_required_sites = [
                site
                for site in get_sonarr_required_hostnames()
                if Config("Hostnames").get(site)
            ]
            if sonarr_required_sites:
                sonarr_config(shared_state, sonarr_required_sites)
                initialize_sonarr_client(shared_state)

        config = Config("JDownloader")
        user = config.get("user")
        password = config.get("password")
        device = config.get("device")

        if not user or not password or not device:
            jdownloader_config(shared_state)

        initialize_notification_settings(shared_state)
        initialize_timeout_slow_mode_settings(shared_state)
        initialize_filecrypt_setting(shared_state)

        api_key = Config("API").get("key")
        if not api_key:
            api_key = shared_state.generate_api_key()
            info("API-Key generated: <g>" + api_key + "</g>")

        print(f"\n===== Quasarr {log_level_names[get_log_level()]} Log =====")

        # Start Logging
        info(f"Web UI: <blue>{shared_state.values['external_address']}</blue>")
        debug(f'Config path: "{config_path}"')

        # Hostnames log
        hostnames_log = []
        set_hostnames_count = 0
        for key in supported_hostnames:
            if key in hostnames:
                hostnames_log.append(
                    f"<bg green><black>{key.upper()}</black></bg green>"
                )
                set_hostnames_count += 1
            else:
                hostnames_log.append(
                    f"<bg black><white>{key.upper()}</white></bg black>"
                )

        total_hostnames_count = len(supported_hostnames)
        if set_hostnames_count == total_hostnames_count:
            count_str = f"<g>{set_hostnames_count}</g>/<g>{total_hostnames_count}</g>"
        else:
            count_str = f"<y>{set_hostnames_count}</y>/<g>{total_hostnames_count}</g>"

        info(f"Hostnames: [{' '.join(hostnames_log)}] {count_str} set")

        protected = shared_state.get_db("protected").retrieve_all_titles()
        if protected:
            package_count = len(protected)
            info(
                f"CAPTCHA-Solution required for <y>{package_count}</y> package{'s' if package_count > 1 else ''} at: "
                f'"{shared_state.values["external_address"]}/captcha"!'
            )

        flaresolverr = multiprocessing.Process(
            target=flaresolverr_checker,
            args=(shared_state_dict, shared_state_lock),
            daemon=True,
        )
        flaresolverr.start()

        jdownloader = multiprocessing.Process(
            target=jdownloader_connection,
            args=(shared_state_dict, shared_state_lock),
            daemon=True,
        )
        jdownloader.start()

        updater = multiprocessing.Process(
            target=update_checker,
            args=(shared_state_dict, shared_state_lock),
            daemon=True,
        )
        updater.start()

        waiter = multiprocessing.Process(
            target=waiting_worker,
            args=(shared_state_dict, shared_state_lock),
            daemon=True,
        )
        waiter.start()

        try:
            get_api(shared_state_dict, shared_state_lock)
        except KeyboardInterrupt:
            sys.exit(0)


def flaresolverr_checker(shared_state_dict, shared_state_lock):
    try:
        shared_state.set_state(shared_state_dict, shared_state_lock)

        # Check if FlareSolverr was previously skipped
        skip_flaresolverr_db = DataBase("skip_flaresolverr")
        flaresolverr_skipped = skip_flaresolverr_db.retrieve("skipped")

        flaresolverr_url = Config("FlareSolverr").get("url")

        # If FlareSolverr is not configured and not skipped, it means it's the first run
        # and the user needs to be prompted via the WebUI.
        # This background process should NOT block or prompt the user.
        # It should only check and log the status.
        if not flaresolverr_url and not flaresolverr_skipped:
            info("FlareSolverr URL not configured. Please configure it via the WebUI.")
            info("Some sites (AL) will not work without FlareSolverr.")
            return  # Exit the checker, it will be re-checked if user configures it later

        if flaresolverr_skipped:
            info("FlareSolverr setup skipped by user preference")
            info(
                "Some sites (AL) will not work without FlareSolverr. Configure it later in the web UI."
            )
        elif flaresolverr_url:
            debug(f"Checking FlareSolverr at URL: <blue>{flaresolverr_url}</blue>")
            flaresolverr_version_checked = check_flaresolverr(
                shared_state, flaresolverr_url
            )
            if flaresolverr_version_checked:
                info(
                    f"FlareSolverr connection successful: <g>v.{flaresolverr_version_checked}</g>"
                )
                debug(
                    f"Using Flaresolverr's User-Agent: <g>{shared_state.values['user_agent']}</g>"
                )
            else:
                error("FlareSolverr check failed - using fallback user agent")
                # Fallback user agent is already set in main process, but we log it
                info(f'User Agent (fallback): "{FALLBACK_USER_AGENT}"')

    except KeyboardInterrupt:
        pass
    except Exception as e:
        error(f"An unexpected error occurred in FlareSolverr checker: {e}")


def waiting_worker(shared_state_dict, shared_state_lock):
    """
    Background worker (Maja fork, F3): retries grabs parked while their host was
    banned, learning the real ban duration, and drains the rest on unban. Also runs
    the F5b history hygiene. One probe per host per tick; conservative pacing so a
    probe never re-triggers the ban for the whole backlog.
    """
    try:
        shared_state.set_state(shared_state_dict, shared_state_lock)
        from quasarr.providers import host_bans
        from quasarr.downloads import retry_waiting_package
        from quasarr.downloads.packages import run_history_hygiene

        TICK = 10
        HYGIENE_EVERY = 30  # ~5 min
        tick = 0
        while True:
            tick += 1
            try:
                now = time.time()

                # Async addurl (Maja fork): process freshly-queued 'pending' grabs in the
                # background so addurl returns instantly. Sequential per host (AL CAPTCHA
                # is serialized); if a host bans mid-run, stop draining it this tick so we
                # don't hammer it — the ban-probe loop below takes over.
                pending = [
                    (pid, ctx)
                    for pid, ctx in host_bans.all_waiting()
                    if ctx.get("pending")
                ]
                pending.sort(key=lambda kv: kv[1].get("created_at", 0))
                banned_this_tick = set()
                for pid, ctx in pending:
                    hk = (ctx.get("source_key") or "").lower()
                    if hk in banned_this_tick or host_bans.is_banned(hk, time.time()):
                        continue
                    debug(f"waiting_worker: processing pending {pid} ({hk})")
                    result = retry_waiting_package(shared_state, pid) or {}
                    if result.get("waiting"):
                        # got banned while processing -> _park_banned_grab recorded it
                        banned_this_tick.add(hk)
                        host_bans.record_probe_failure(hk, time.time())

                now = time.time()
                # Work per host that still has parked packages. A banned host is only
                # probed when due; an unbanned host with leftover packages is drained
                # one-per-tick (the 30s tick spaces the drain so it can't instantly
                # re-trigger the ban for the whole backlog).
                hosts = {
                    (ctx.get("source_key") or "").lower()
                    for _pid, ctx in host_bans.all_waiting()
                }
                for source_key in hosts:
                    if not source_key:
                        continue
                    banned = host_bans.is_banned(source_key, now)
                    if banned and not host_bans.due_for_probe(source_key, now):
                        continue
                    item = host_bans.oldest_waiting_for_host(source_key)
                    if not item:
                        if banned:
                            host_bans.record_unban(source_key, now)
                        continue
                    package_id, _ctx = item
                    debug(
                        f"waiting_worker: {'probing' if banned else 'draining'} "
                        f"{source_key} with {package_id}"
                    )
                    result = retry_waiting_package(shared_state, package_id) or {}
                    if result.get("waiting"):
                        # Re-parked (host still/again banned). _park_banned_grab already
                        # recorded the ban; schedule the next probe with back-off.
                        host_bans.record_probe_failure(source_key, now)
                        info(f"waiting_worker: {source_key} still banned, backing off")
                    elif banned:
                        learned = host_bans.record_unban(source_key, now)
                        info(
                            f"waiting_worker: {source_key} unbanned "
                            f"(learned ~{int((learned or 0) / 60)}min); draining backlog"
                        )

                # Give-up sweep: park packages older than the 48h cap fail honestly.
                for package_id, ctx in host_bans.all_waiting():
                    age = now - ctx.get("created_at", now)
                    if age > host_bans.WAITING_MAX_AGE_S:
                        host_bans.delete_waiting(package_id)
                        from quasarr.downloads import fail

                        fail(
                            ctx.get("title", package_id),
                            package_id,
                            shared_state,
                            reason="host ban did not lift within 48h",
                        )
                        info(f"waiting_worker: gave up on {package_id} after 48h")

                if tick % HYGIENE_EVERY == 0:
                    run_history_hygiene(shared_state)
            except Exception as e:
                error(f"waiting_worker tick failed: {e}")
            time.sleep(TICK)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        error(f"waiting_worker crashed: {e}")


def update_checker(shared_state_dict, shared_state_lock):
    try:
        shared_state.set_state(shared_state_dict, shared_state_lock)

        message = "!!! UPDATE AVAILABLE !!!"
        link = "https://github.com/rix1337/Quasarr/releases/latest"

        shared_state.update("last_checked_version", f"v.{version.get_version()}")

        while True:
            try:
                update_available = version.newer_version_available()
            except Exception as e:
                error(
                    f"Error getting latest version: {e}!\nPlease manually check: <blue>{link}</blue> for more information!"
                )
                update_available = None

            if (
                update_available
                and shared_state.values["last_checked_version"] != update_available
            ):
                shared_state.update("last_checked_version", update_available)
                info(message)
                info(f"Please update to {update_available} as soon as possible!")
                info(f'Release notes at: "{link}"')
                update_available = {"version": update_available, "link": link}
                send_notification(
                    shared_state,
                    message,
                    NotificationType.QUASARR_UPDATE,
                    details=update_available,
                )

            # wait one hour before next check
            time.sleep(60 * 60)
    except KeyboardInterrupt:
        pass


def jdownloader_connection(shared_state_dict, shared_state_lock):
    try:
        shared_state.set_state(shared_state_dict, shared_state_lock)

        while True:
            shared_state.set_device_from_config()

            device = shared_state.get_device()

            try:
                info(f"Connection to JDownloader successful: <g>{device.name}</g>")
            except Exception as e:
                crit(f"Error connecting to JDownloader: {e}! Stopping Quasarr...")
                sys.exit(1)

            try:
                shared_state.set_device_settings()
            except Exception as e:
                error(f"Error checking settings: {e}")

            try:
                shared_state.update_jdownloader()
            except Exception as e:
                error(f"Error updating JDownloader: {e}")

            try:
                shared_state.start_downloads()
            except Exception as e:
                error(f"Error starting downloads: {e}")

            while True:
                time.sleep(300)
                device_state = shared_state.check_device(
                    shared_state.values.get("device")
                )
                if not device_state:
                    error("Lost connection to JDownloader. Reconnecting...")
                    shared_state.update("device", False)
                    break

    except KeyboardInterrupt:
        pass
