import os
import logging
import queue
import signal
import sys
import time
import threading
from typing import Optional
from bot.TeamTalk.structs import Message, MessageType, User, UserType, UserStatusMode, UserState
from bot import errors

from pydantic import ValidationError

from bot import (
    TeamTalk,
    cache,
    commands,
    config,
    connectors,
    logger,
    modules,
    player,
    services,
    sound_devices,
    translator,
    app_vars,
)
from bot.auth import redaction
from bot.auth.session import AuthJobManager
from bot.auth.store import SecretStore
from bot.modules.auth_portal import AuthPortal


class Bot:
    def __init__(
        self,
        config_file_name: Optional[str],
        cache_file_name: Optional[str] = None,
        log_file_name: Optional[str] = None,
    ) -> None:
        try:
            self.config_manager = config.ConfigManager(config_file_name)
        except ValidationError as e:
            for error in e.errors():
                print(
                    "Error in config:",
                    ".".join([str(i) for i in error["loc"]]),
                    error["msg"],
                )
            sys.exit(1)
        except PermissionError:
            sys.exit(
                "The configuration file cannot be accessed due to a permission error or is already used by another instance of the bot"
            )
        self.config = self.config_manager.config
        self.translator = translator.Translator(self.config.general.language)
        try:
            if cache_file_name:
                self.cache_manager = cache.CacheManager(cache_file_name)
            else:
                cache_file_name = self.config.general.cache_file_name
                if not os.path.isdir(
                    os.path.join(*os.path.split(cache_file_name)[0:-1])
                ):
                    cache_file_name = os.path.join(
                        self.config_manager.config_dir, cache_file_name
                    )
                self.cache_manager = cache.CacheManager(cache_file_name)
        except PermissionError:
            sys.exit(
                "The cache file cannot be accessed due to a permission error or is already used by another instance of the bot"
            )
        self.cache = self.cache_manager.cache
        self.log_file_name = log_file_name
        self.player = player.Player(self)
        self.ttclient = TeamTalk.TeamTalk(self)
        self.tt_player_connector = connectors.TTPlayerConnector(self)
        self.sound_device_manager = sound_devices.SoundDeviceManager(self)
        self.service_manager = services.ServiceManager(self)
        self.module_manager = modules.ModuleManager(self)
        self.command_processor = commands.CommandProcessor(self)
        
        # JC command tracking
        self.jc_requested_by_user_id: Optional[int] = None
        self.default_channel = self.config.teamtalk.channel
        self.is_updating = False
        self.auth_portal = None

    def initialize(self):
        if self.config.logger.log:
            logger.initialize_logger(self)
        # Installed immediately after the handlers exist and before anything
        # touches a credential, so there is no window in which a password could
        # reach a log file.
        redaction.install()
        logging.debug("Initializing")
        self.sound_device_manager.initialize()
        self.ttclient.initialize()
        self.player.initialize()
        self.service_manager.initialize()
        self._initialize_auth_portal()
        logging.debug("Initialized")

    def _initialize_auth_portal(self) -> None:
        """Start the portal and register its stored secrets for redaction.

        A failure here disables account connection but must never stop the bot
        reaching TeamTalk: YouTube, direct URLs and everything already connected
        keep working.
        """
        config = getattr(self.config, "auth_portal", None)
        if config is None or not config.enabled:
            logging.info("The account portal is switched off in the configuration.")
            return
        try:
            secrets_dir = os.path.join(self.config_manager.config_dir, "secrets")
            store = SecretStore(secrets_dir)
            # Anything already stored goes into the filter now, so a credential
            # saved in an earlier run cannot surface in this run's logs.
            redaction.get_filter().register_all(store.values_to_redact())

            youtube_bridge = None
            for name in ("yt", "ytm"):
                service = self.service_manager.services.get(name)
                bridge = getattr(service, "_bridge", None)
                if bridge is not None:
                    youtube_bridge = bridge
                    break

            self.auth_portal = AuthPortal(
                translator=self.translator,
                store=store,
                jobs=AuthJobManager(),
                config=config,
                locale=self.config.general.language,
                youtube_bridge=youtube_bridge,
            )
            self.auth_portal.start()
            self.command_processor.auth_portal = self.auth_portal
        except Exception as error:
            logging.error(f"The account portal could not start: {error}", exc_info=True)
            self.auth_portal = None

    def run(self):
        logging.debug("Starting")
        try:
            signal.signal(signal.SIGTERM, lambda signum, frame: self.close())
        except Exception as e:
            logging.warning(f"Could not register SIGTERM handler: {e}")
        self.player.run()
        self.tt_player_connector.start()
        self.command_processor.run()
        logging.info("Started")
        logging.info(f"Processing {len(self.config.general.start_commands)} startup command(s)...")
        startup_context_user = User(
            id=-1, nickname="Startup", username="", 
            channel=self.ttclient.channel, 
            type=UserType.Admin, is_admin=True, 
            status="", gender=UserStatusMode.N, state=UserState.Null, 
            client_name="", version=0, user_account=None, is_banned=False
        )
        for command in self.config.general.start_commands:
            message = Message(text=command, user=startup_context_user, channel=self.ttclient.channel, type=MessageType.User)
            self.command_processor(message)
        
        # Check for update success file
        success_file = os.path.join(self.config_manager.config_dir, "update_success")
        if os.path.exists(success_file):
            try:
                time.sleep(2)
                msg = self.translator.translate("Hello this is the streamer bot here, I have finished updates, I am now back online, and ready for streaming again. Thank you for being patient.")
                self.ttclient.send_message(msg, type=2)
            except Exception as e:
                logging.error(f"Error sending update success message: {e}")
            try:
                os.remove(success_file)
            except Exception:
                pass

        # Periodic Pre-warming tracking
        self.last_pre_warm_time = time.time()
        self.pre_warm_interval = 50 # 50 seconds (User request: 'oi' every 50s)

        self._close = False
        while not self._close:
            try:
                message = self.ttclient.message_queue.get_nowait()
                logging.info(
                    "New message {text} from {username}".format(
                        text=message.text, username=message.user.username
                    )
                )
                self.command_processor(message)
            except queue.Empty:
                pass
            
            # Check for periodic pre-warming
            if time.time() - self.last_pre_warm_time >= self.pre_warm_interval:
                self.last_pre_warm_time = time.time()
                threading.Thread(target=self._perform_periodic_pre_warm, daemon=True).start()

            # Check for update trigger file
            update_file = os.path.join(self.config_manager.config_dir, "update_in_progress")
            if os.path.exists(update_file):
                self.is_updating = True
                try:
                    msg = self.translator.translate("Hello, this is the streamer bot here, just wanted to let you know a update is running and I will restart shortly.")
                    self.ttclient.send_message(msg, type=2)
                except Exception as e:
                    logging.error(f"Error sending update warning: {e}")
                try:
                    os.remove(update_file)
                except Exception:
                    pass

            time.sleep(app_vars.loop_timeout)

    def _perform_periodic_pre_warm(self):
        logging.info("Starting periodic pre-warming for services...")
        try:
            # Pre-warm YouTube if enabled
            yt = self.service_manager.get_service_by_name("yt")
            if yt and hasattr(yt, "_pre_warm"):
                yt._pre_warm()
            
            # Pre-warm YouTube Music if enabled
            ytm = self.service_manager.get_service_by_name("ytm")
            if ytm and hasattr(ytm, "_pre_warm"):
                ytm._pre_warm()
                
            logging.info("Periodic pre-warming cycle completed.")
        except Exception as e:
            logging.error(f"Error during periodic pre-warming: {e}")

    def close(self) -> None:
        logging.debug("Closing bot")
        if getattr(self, "is_updating", False):
            try:
                msg = self.translator.translate("The bot is restarting now to apply the update. See you in a moment!")
                self.ttclient.send_message(msg, type=2)
                time.sleep(0.5)
            except Exception as e:
                logging.error(f"Error sending shutdown message: {e}")
        if self.auth_portal is not None:
            try:
                # Also revokes every live token, so a link pasted somewhere does
                # not survive a restart.
                self.auth_portal.close()
            except Exception as e:
                logging.warning(f"Error closing the account portal: {e}")
        self.player.close()
        self.ttclient.close()
        self.tt_player_connector.close()
        self.config_manager.close()
        self.cache_manager.close()
        self._close = True
        logging.info("Bot closed")