import traceback
import requests
import json
import os
from typing import Any, Optional
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
import time
from collections import defaultdict

from base_plugin import BasePlugin, MethodReplacement, HookResult, HookStrategy
from hook_utils import find_class

from client_utils import run_on_queue, get_messages_controller, get_last_fragment
from android_utils import run_on_ui_thread, log
from ui.bulletin import BulletinHelper
from ui.alert import AlertDialogBuilder
from ui.settings import Header, Input, Selector, Divider, Text, Switch

DEBUG_TRANSLATION_LOGS = True

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="translator_")

_session = None

_in_flight_translations = {}  # key -> Future mapping
_in_flight_lock = __import__('threading').Lock()

CACHE_DIR = os.path.expanduser("~/.ayugram_plugin_cache")
os.makedirs(CACHE_DIR, exist_ok=True)
TRANSLATIONS_CACHE_FILE = os.path.join(CACHE_DIR, "translations_cache.json")

_offline_state = False
_last_offline_check = 0

in_progress_messages = set()

_translation_cache = {}
_cache_dirty = False
_cache_last_save = 0
_cache_save_interval = 60

_plugin_instance = None

_request_cache = {}  # text_lang_key -> result/pending
_request_lock = __import__('threading').Lock()

_visible_message_ids = set()
_viewport_lock = __import__('threading').Lock()

def update_visible_messages(visible_ids):
    """Update which messages are currently visible on screen."""
    global _visible_message_ids
    with _viewport_lock:
        _visible_message_ids = set(visible_ids) if visible_ids else set()

def is_message_visible(message_id):
    """Check if a message is currently visible on screen."""
    with _viewport_lock:
        return message_id in _visible_message_ids

def load_cache():
    """Load translations cache from file into memory only once."""
    global _translation_cache
    if _translation_cache:
        return _translation_cache
    
    if os.path.exists(TRANSLATIONS_CACHE_FILE):
        try:
            with open(TRANSLATIONS_CACHE_FILE, 'r', encoding='utf-8') as f:
                _translation_cache = json.load(f)
                return _translation_cache
        except Exception as e:
            log(f"[CACHE] Failed to load cache: {e}")
    
    _translation_cache = {}
    return _translation_cache

def save_cache(cache_data):
    """Save translations cache to file only when dirty and interval passed."""
    global _cache_dirty, _cache_last_save
    _cache_dirty = True
    current_time = time.time()
    
    if current_time - _cache_last_save < _cache_save_interval:
        return
    
    _cache_last_save = current_time
    try:
        with open(TRANSLATIONS_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)
        _cache_dirty = False
    except Exception as e:
        log(f"[CACHE] Failed to save cache: {e}")

def is_online() -> bool:
    """Check if device has internet connection with better caching."""
    global _offline_state, _last_offline_check
    current_time = time.time()
    
    if current_time - _last_offline_check < 10:
        return not _offline_state
    
    _last_offline_check = current_time
    try:
        response = get_session().get("https://1.1.1.1", timeout=1)
        _offline_state = False
        return True
    except Exception:
        _offline_state = True
        return False

def get_cache_key(text: str, target_lang: str) -> str:
    """Generate cache key from text and target language."""
    return f"{text[:50]}_{target_lang}"

def get_cache_size():
    """Get the size of the translations cache in KB."""
    try:
        if os.path.exists(TRANSLATIONS_CACHE_FILE):
            size_bytes = os.path.getsize(TRANSLATIONS_CACHE_FILE)
            return size_bytes / 1024
        return 0
    except Exception:
        return 0

def get_cache_entry_count():
    """Get the number of cached translations."""
    try:
        cache_data = load_cache()
        return len(cache_data)
    except Exception:
        return 0

def _get_telegram_target_language():
    """Get target language from TranslateController per-dialog, or global settings."""
    try:
        messages_controller = get_messages_controller()
        if messages_controller:
            translate_controller = messages_controller.getTranslateController()
            if translate_controller:
                fragment = get_last_fragment()
                if fragment and hasattr(fragment, 'getDialogId'):
                    dialog_id = fragment.getDialogId()
                    dialog_lang = translate_controller.getDialogTranslateTo(dialog_id)
                    if dialog_lang:
                        return dialog_lang
    except Exception as e:
        log(f"[LANG] Failed to read dialog language: {e}")
    
    try:
        MessagesController_class = jclass("org.telegram.messenger.MessagesController")
        settings = MessagesController_class.getGlobalMainSettings()
        telegram_lang = settings.getString("translate_to_language", None)
        if telegram_lang:
            return telegram_lang
    except Exception:
        pass
    
    return "en"

def _clear_native_translations():
    """
    Clear translations from NATIVE Telegram storage (messageObject.messageOwner.translatedText)
    This ensures translations are removed from Telegram's internal storage, not just the cache.
    """
    try:
        messages_controller = get_messages_controller()
        if not messages_controller:
            return
        
        fragment = get_last_fragment()
        if not fragment or not hasattr(fragment, 'getDialogId'):
            return
        
        dialog_id = fragment.getDialogId()
        
        # Get all messages in dialog and clear their translatedText
        try:
            translate_controller = messages_controller.getTranslateController()
            if translate_controller:
                # Clear by disabling/enabling translation for dialog
                translate_controller.toggleTranslatingDialog(dialog_id, False)
                translate_controller.toggleTranslatingDialog(dialog_id, True)
                log("[NATIVE] Cleared native Telegram translations")
        except Exception as e:
            log(f"[NATIVE] Error clearing native translations: {e}")
        
        # Post UI notification to refresh all messages
        try:
            NotificationCenter.getInstance(messages_controller.getCurrentAccount()).postNotificationName(
                NotificationCenter.updateInterfaces, JInteger(4)
            )
        except Exception as e:
            log(f"[UI] Failed to post refresh notification: {e}")
    
    except Exception as e:
        log(f"[NATIVE] Error in _clear_native_translations: {e}")

def _clear_translation_cache_with_native(view):
    """Clear both plugin cache AND native Telegram translation storage."""
    try:
        # Get cache info before clearing
        cache_size = get_cache_size()
        entry_count = get_cache_entry_count()
        
        # Clear native Telegram translations first
        _clear_native_translations()
        
        # Then clear plugin cache file
        if os.path.exists(TRANSLATIONS_CACHE_FILE):
            os.remove(TRANSLATIONS_CACHE_FILE)
            success_msg = f"Cache cleared! Removed {entry_count} entries (~{cache_size:.1f}KB)"
            BulletinHelper.show_success(success_msg)
            log(f"[CACHE] {success_msg}")
        else:
            BulletinHelper.show_info("Translation cache is already empty")
            log("[CACHE] Cache is empty")
    except Exception as e:
        log(f"Failed to clear translation cache: {e}")
        BulletinHelper.show_error(f"Failed to clear cache: {str(e)}")

def is_already_translated(original_text: str, target_lang_code: str) -> bool:
    """Fast check if text is already in target language."""
    if not original_text or len(original_text) < 3:
        return True
    
    try:
        if target_lang_code in ["zh", "ja", "ko"]:
            for c in original_text:
                if ('\u4e00' <= c <= '\u9fff') or ('\u3040' <= c <= '\u309f') or \
                   ('\u30a0' <= c <= '\u30ff') or ('\uac00' <= c <= '\ud7af'):
                    return True
        
        elif target_lang_code in ["ru", "uk", "bg"]:
            for c in original_text:
                if '\u0400' <= c <= '\u04ff':
                    return True
        
    except:
        pass
    
    return False

def normalize_language_code(lang_code: str) -> str:
    """
    Normalize language code matching Telegram's conversion.
    Handles: nb->no, strips region suffix (zh_CN -> zh), etc.
    """
    if not lang_code:
        return "en"
    
    base_lang = lang_code.split("_")[0].lower()
    
    if base_lang == "nb":
        return "no"
    
    return base_lang

def preserve_entities(original_text: str, translated_text: str) -> str:
    """
    Preserve entities (links, emojis, formatting) from original translation.
    Matches Telegram's TranslateAlert2.preprocess() behavior.
    """
    try:
        import re
        
        # Extract URLs from original text
        url_pattern = r'(https?://[^\s]+)'
        original_urls = re.findall(url_pattern, original_text)
        translated_urls = re.findall(url_pattern, translated_text)
        
        # If translation accidentally removed URLs, restore them
        if original_urls and not translated_urls:
            for url in original_urls:
                if url not in translated_text:
                    # Add URL to end if not present
                    translated_text += f" {url}"
        
        # Preserve emoji patterns (Unicode ranges)
        emoji_pattern = r'[\U0001F300-\U0001F9FF]+'
        
        # Don't remove/change emojis in translation
        original_emojis = set(re.findall(emoji_pattern, original_text))
        translated_emojis = set(re.findall(emoji_pattern, translated_text))
        
        # If emojis were lost, preserve them from original at end
        missing_emojis = original_emojis - translated_emojis
        if missing_emojis:
            translated_text += "".join(missing_emojis)
        
        return translated_text
    except Exception as e:
        log(f"[ENTITY] Error preserving entities: {e}")
        return translated_text

class BeforeHookedTrue:
    def before_hooked_method(self, param):
        param.setResult(True)


class BeforeHookedFalse:
    def before_hooked_method(self, param):
        param.setResult(False)


class LocalPremiumHook:
    def __init__(self):
        self._premium_unhooks = []

    def premium_hooking(self):
        clazz = jclass("org.telegram.messenger.UserConfig")
        unhook1 = self.hook_method(clazz.getClass().getDeclaredMethod("isPremium"), BeforeHookedTrue())
        unhook2 = self.hook_method(clazz.getClass().getDeclaredMethod("hasPremiumOnAccounts"), BeforeHookedTrue())
        clazz2 = jclass("org.telegram.messenger.MessagesController")
        unhook3 = self.hook_method(clazz2.getClass().getDeclaredMethod("premiumFeaturesBlocked"), BeforeHookedFalse())
        self._premium_unhooks = [unhook1, unhook2, unhook3]

    def premium_unhooking(self):
        for unhook in getattr(self, "_premium_unhooks", []):
            if unhook:
                try:
                    self.unhook_method(unhook)
                except Exception:
                    pass
        self._premium_unhooks = []


class AdvancedTranslatorPlugin(BasePlugin, LocalPremiumHook):
    """The main class for the Advanced Translator plugin."""

    _global_generation = 0

    def on_plugin_load(self):
        global _plugin_instance
        _plugin_instance = self
        
        if not JAVA_CLASSES_FOUND:
            BulletinHelper.show_error("Translator: Failed to load (core classes not found).")
            return
        
        try:
            self.premium_hooking()
            log("Translator: Premium features unlocked automatically")
        except Exception as e:
            log(str(e))
        
        try:
            target_method = None
            translate_controller_class = TranslateController.getClass()
            log("Translator: Attempting to hook 'pushToTranslate'...")
            try:
                target_method = translate_controller_class.getDeclaredMethod("pushToTranslate", MessageObject, String, Callback3)
                log("Translator: Successfully found method 'pushToTranslate'.")
            except Exception as e:
                log(f"Translator [ERROR]: Failed to find 'pushToTranslate'. Error: {e}")
                target_method = None
            if target_method:
                target_method.setAccessible(True)
                AdvancedTranslatorPlugin._global_generation += 1
                self.translate_hook_instance = TranslateHook(self, AdvancedTranslatorPlugin._global_generation)
                self.hook_method(target_method, self.translate_hook_instance)
            else:
                BulletinHelper.show_error("Translator: Failed to find the target translation method to hook.")
        except Exception:
            log(f"Translator [FATAL]: An exception occurred during hooking: {traceback.format_exc()}")
            BulletinHelper.show_error("Translator: An error occurred while hooking.")
        self.add_on_send_message_hook()

    def on_send_message_hook(self, account, params):
        global DEBUG_TRANSLATION_LOGS
        if not hasattr(params, "message") or not isinstance(params.message, str):
            return None
        msg = params.message.strip()
        if msg.lower() == "!debug":
            DEBUG_TRANSLATION_LOGS = not DEBUG_TRANSLATION_LOGS
            state = "ENABLED" if DEBUG_TRANSLATION_LOGS else "DISABLED"
            BulletinHelper.show_info(f"Translation debug logs {state}")
            return HookResult(strategy=HookStrategy.CANCEL)
        return None

    def on_plugin_unload(self):
        log("Unloaded")

    def _on_premium_toggle(self, value):
        pass

    def create_settings(self):
        settings_list = []
        
        settings_list.append(Divider())
        settings_list.append(Header(text="Translation Provider"))
        settings_list.append(
            Selector(
                key="api_provider",
                text="Provider",
                default=2,
                items=["Google Translate", "DeepL", "Delirius"],
                icon="msg_translate",
            )
        )
        
        provider_index = self.get_setting("api_provider", 2)
        if provider_index == 1:
            settings_list.append(
                Input(
                    key="deepl_api_key",
                    text="DeepL API Key",
                    default=self.get_setting("deepl_api_key", ""),
                    icon="msg_pin",
                    subtext="Get key from deepl.com/pro",
                )
            )
        
        
        settings_list.append(Divider())
        settings_list.append(Header(text="Cache Management"))
        
        cache_size = get_cache_size()
        entry_count = get_cache_entry_count()
        
        settings_list.append(
            Text(
                text=f"Cache: {entry_count} entries (~{cache_size:.1f}KB)",
                icon="msg_storage",
            )
        )
        
        settings_list.append(
            Text(
                text="Clear Translation Cache",
                icon="msg_delete",
                red=True,
                on_click=_clear_translation_cache_with_native
            )
        )
        
        settings_list.append(Divider())
        
        return settings_list


class TranslateHook(MethodReplacement):
    """Replaces the original translation method in TranslateController with custom providers."""

    def __init__(self, plugin_instance: AdvancedTranslatorPlugin, generation: int):
        super().__init__()
        self.plugin = plugin_instance
        self._generation = generation
        self._translation_count = 0

    def cleanup(self):
        log(f"[PLUGIN] TranslateHook.cleanup: (gen {self._generation}).")

    def _show_error_dialog(self, message: str):
        try:
            fragment = get_last_fragment()
            if not (fragment and fragment.getParentActivity()):
                return
            builder = AlertDialogBuilder(fragment.getParentActivity())
            builder.set_title("Translator Error")
            builder.set_message(message)

            def on_copy_error_click(b, w):
                AndroidUtilities = jclass("org.telegram.messenger.AndroidUtilities")
                AndroidUtilities.addToClipboard(message)
                BulletinHelper.show_info("copied_to_clipboard")
                b.dismiss()

            builder.set_negative_button("close_button", lambda b, w: b.dismiss())
            builder.set_positive_button("copy_button", on_copy_error_click)
            builder.show()

        except Exception as e:
            log(f"Failed to show error dialog: {e}")
            BulletinHelper.show_error("An error occurred, but the dialog could not be displayed.")

    def replace_hooked_method(self, param: Any) -> Any:
        """Hooks into Telegram's pushToTranslate method."""
        try:
            message_object = param.args[0]
            if not message_object or not isinstance(message_object, MessageObject):
                return None

            mo = getattr(message_object, "messageOwner", None)
            if not mo:
                return None

            message_id = message_object.getId()
            if not is_message_visible(message_id):
                if DEBUG_TRANSLATION_LOGS:
                    log(f"[SKIP] Message {message_id} not visible on screen")
                return None

            # Check if already translated to target language
            if hasattr(mo, 'translatedText') and mo.translatedText is not None:
                target_lang = _get_telegram_target_language()
                if hasattr(mo, 'translatedToLanguage') and mo.translatedToLanguage == target_lang:
                    if DEBUG_TRANSLATION_LOGS:
                        log(f"[SKIP] Already translated to {target_lang}")
                    return None

            original_text = message_object.messageText
            used_message_owner = False
            if (not original_text or not isinstance(original_text, str) or not original_text.strip()) and hasattr(mo, "message") and mo.message:
                original_text = mo.message
                used_message_owner = True
            if not original_text or not isinstance(original_text, str) or not original_text.strip():
                return None

            if message_object.isOut():
                return None

            message_key = f"{message_object.getId()}"
            
            if message_key in in_progress_messages:
                return None
            
            in_progress_messages.add(message_key)
            
            target_lang_code = normalize_language_code(_get_telegram_target_language())
            
            if is_already_translated(original_text, target_lang_code):
                if DEBUG_TRANSLATION_LOGS:
                    log(f"[SKIP] Already {target_lang_code}")
                in_progress_messages.discard(message_key)
                return None

            cache_key = get_cache_key(original_text, target_lang_code)
            with _request_lock:
                if cache_key in _request_cache:
                    # Request already pending or completed
                    if DEBUG_TRANSLATION_LOGS:
                        log(f"[DEDUP] Translation in-flight for {cache_key[:20]}")
                    in_progress_messages.discard(message_key)
                    return None
                
                # Mark as in-flight
                _request_cache[cache_key] = "PENDING"
                
                future = _executor.submit(
                    self._process_translation,
                    original_text,
                    param,
                    used_message_owner,
                    message_key,
                    target_lang_code,
                    message_object,
                    cache_key  # Pass cache_key for cleanup
                )

        except Exception as e:
            self._handle_error(e, "replace_hooked_method")
        return None

    def _process_translation(self, original_text: str, param: Any, used_message_owner: bool, message_key: str, target_lang_code: str, message_object=None, cache_key=None):
        """Main translation processing - stores in NATIVE Telegram storage."""
        try:
            if not cache_key:
                cache_key = get_cache_key(original_text, target_lang_code)
            
            if not is_message_visible(message_object.getId()):
                if DEBUG_TRANSLATION_LOGS:
                    log(f"[SKIP] Message became invisible during processing")
                in_progress_messages.discard(message_key)
                return
            
            cache_data = load_cache()
            
            if cache_key in cache_data:
                cached_translation = cache_data[cache_key]
                if DEBUG_TRANSLATION_LOGS:
                    log(f"[CACHE HIT] {cache_key[:20]}")
                
                cached_translation = preserve_entities(original_text, cached_translation)
                
                self._store_translation_in_native_storage(
                    message_object,
                    cached_translation,
                    target_lang_code
                )
                in_progress_messages.discard(message_key)
                return
            
            if not is_online():
                if DEBUG_TRANSLATION_LOGS:
                    log(f"[OFFLINE] Skipping translation...")
                in_progress_messages.discard(message_key)
                return
            
            provider_index = self.plugin.get_setting("api_provider", 2)
            provider_name = TRANSLATION_PROVIDERS.get(provider_index, "delirius")

            start_time = time.time()
            
            translated_text = None
            if provider_name == "google":
                translated_text = self._translate_with_google(original_text, target_lang_code)
            elif provider_name == "deepl":
                translated_text = self._translate_with_deepl(original_text, target_lang_code)
            elif provider_name == "delirius":
                translated_text = self._translate_with_delirius(original_text, target_lang_code)
            
            elapsed = time.time() - start_time

            if not translated_text:
                if DEBUG_TRANSLATION_LOGS:
                    log(f"[FAIL] {provider_name.upper()} in {elapsed:.2f}s")
                in_progress_messages.discard(message_key)
                return

            translated_text = preserve_entities(original_text, translated_text)

            cache_data[cache_key] = translated_text
            save_cache(cache_data)
            
            if DEBUG_TRANSLATION_LOGS:
                log(f"[{provider_name.upper()}] {elapsed:.2f}s")

            self._store_translation_in_native_storage(
                message_object,
                translated_text,
                target_lang_code
            )

        except Exception as e:
            self._handle_error(e, "_process_translation")

        finally:
            in_progress_messages.discard(message_key)
            if cache_key:
                with _request_lock:
                    _request_cache.pop(cache_key, None)

    def _store_translation_in_native_storage(self, message_object, translated_text: str, target_lang_code: str):
        """
        Stores translation in NATIVE Telegram storage exactly like TranslateAlert2.
        Uses messageObject.messageOwner.translatedText which is Telegram's native field.
        This triggers the shimmer animation automatically when UI updates.
        """
        def ui_update_task():
            try:
                mo = getattr(message_object, "messageOwner", None)
                if not mo:
                    return
                
                text_with_entities = TLRPC.TL_textWithEntities()
                text_with_entities.text = translated_text
                text_with_entities.entities = ArrayList()
                
                mo.translatedText = text_with_entities
                mo.translatedToLanguage = target_lang_code
                
                account = message_object.currentAccount if hasattr(message_object, 'currentAccount') else 0
                
                # Post notifications to trigger UI update and shimmer animation
                NotificationCenter.getInstance(account).postNotificationName(
                    NotificationCenter.messageTranslated, message_object
                )
                
                NotificationCenter.getInstance(account).postNotificationName(
                    NotificationCenter.updateInterfaces, JInteger(4)
                )
                
            except Exception as e:
                log(f"[UI] Failed to store translated text: {e}")

        run_on_ui_thread(ui_update_task)

    def _translate_with_google(self, text: str, target_lang: str) -> Optional[str]:
        try:
            params = {"client": "gtx", "sl": "auto", "tl": target_lang, "dt": "t", "q": text}
            headers = {"User-Agent": get_random_user_agent()}
            
            response = get_session().get(
                "https://clients5.google.com/translate_a/t",
                params=params,
                headers=headers,
                timeout=1.0
            )
            
            if response.status_code == 429:
                if DEBUG_TRANSLATION_LOGS:
                    log("[GOOGLE] Rate limited (429)")
                return None
            
            response.raise_for_status()
            result = response.json()
            if result and isinstance(result, list) and result[0] and isinstance(result[0], list):
                return result[0][0]
            return None
        except Exception as e:
            if DEBUG_TRANSLATION_LOGS:
                log(f"[GOOGLE] {str(e)[:40]}")
            return None

    def _translate_with_delirius(self, text: str, target_lang: str) -> Optional[str]:
        try:
            url = "https://delirius-apiofc.vercel.app/tools/translate"
            params = {"text": text, "language": target_lang}
            headers = {"User-Agent": get_random_user_agent()}
            
            response = get_session().get(url, params=params, headers=headers, timeout=1.2)
            
            if response.status_code == 429:
                if DEBUG_TRANSLATION_LOGS:
                    log("[DELIRIUS] Rate limited (429)")
                return None
            
            response.raise_for_status()
            data = response.json()
            
            if data.get("status") and data.get("data"):
                return data["data"]
            return None
        except Exception as e:
            if DEBUG_TRANSLATION_LOGS:
                log(f"[DELIRIUS] {str(e)[:40]}")
            return None

    def _translate_with_deepl(self, text: str, target_lang: str) -> Optional[str]:
        try:
            api_key = self.plugin.get_setting("deepl_api_key", "").strip()
            if not api_key:
                if DEBUG_TRANSLATION_LOGS:
                    log("[DEEPL] No API key configured")
                return None
            
            deepl_target = DEEPL_LANG_MAP.get(target_lang.lower(), target_lang.upper())
            url = "https://api-free.deepl.com/v2/translate"
            headers = {
                "Authorization": f"DeepL-Auth-Key {api_key}",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": get_random_user_agent(),
            }
            data = {"text": text, "target_lang": deepl_target}
            
            response = get_session().post(url, headers=headers, data=data, timeout=1.2)
            
            if response.status_code == 429:
                if DEBUG_TRANSLATION_LOGS:
                    log("[DEEPL] Rate limited (429)")
                return None
            
            if response.status_code == 200:
                resp_json = response.json()
                if resp_json.get("translations"):
                    return resp_json["translations"][0]["text"]
            return None
        except Exception as e:
            if DEBUG_TRANSLATION_LOGS:
                log(f"[DEEPL] {str(e)[:40]}")
            return None

TRANSLATION_PROVIDERS = {
    0: "google",
    1: "deepl",
    2: "delirius",
}

LANG_CODE_MAP = {
    "Bulgarian": "bg", "Czech": "cs", "Danish": "da", "German": "de", "Greek": "el",
    "English": "en", "Spanish": "es", "Estonian": "et", "Finnish": "fi", "French": "fr",
    "Hungarian": "hu", "Indonesian": "id", "Italian": "it", "Japanese": "ja", "Korean": "ko",
    "Lithuanian": "lt", "Latvian": "lv", "Norwegian (Bokmål)": "nb", "Dutch": "nl", "Polish": "pl",
    "Portuguese": "pt", "Romanian": "ro", "Russian": "ru", "Slovak": "sk", "Slovenian": "sl",
    "Swedish": "sv", "Turkish": "tr", "Ukrainian": "uk", "Chinese (simplified)": "zh",
}

DEEPL_LANG_MAP = {
    "en": "EN", "de": "DE", "fr": "FR", "es": "ES", "it": "IT", "nl": "NL",
    "pl": "PL", "ru": "RU", "ja": "JA", "zh": "ZH", "bg": "BG", "cs": "CS",
    "da": "DA", "el": "EL", "et": "ET", "fi": "FI", "hu": "HU", "lt": "LT",
    "lv": "LV", "pt": "PT", "ro": "RO", "sk": "SK", "sl": "SL", "sv": "SV",
    "tr": "TR", "uk": "UK", "ko": "KO", "nb": "NB",
}

USER_AGENTS = [
    "Mozilla/5.0 (Linux; Android 11; Pixel 5) AppleWebKit/537.36",
    "Mozilla/5.0 (Linux; Android 10; SM-G973F) AppleWebKit/537.36",
    "Mozilla/5.0 (Linux; Android 9; Mi 9T Pro) AppleWebKit/537.36",
]


def get_session():
    """Get or create global session with connection pooling."""
    global _session
    if _session is None:
        _session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=8,
            pool_maxsize=8,
            max_retries=0
        )
        _session.mount('http://', adapter)
        _session.mount('https://', adapter)
    return _session


def get_random_user_agent():
    import random
    return random.choice(USER_AGENTS)


JAVA_CLASSES_FOUND = False

try:
    from java import jclass
    from java.lang import Integer as JInteger
    from java.util import ArrayList

    MessagesController = jclass("org.telegram.messenger.MessagesController")
    TranslateController = jclass("org.telegram.messenger.TranslateController")
    MessageObject = jclass("org.telegram.messenger.MessageObject")
    String = jclass("java.lang.String")
    Utilities = jclass("org.telegram.messenger.Utilities")
    Callback3 = Utilities.Callback3
    TLRPC = jclass("org.telegram.tgnet.TLRPC")
    NotificationCenter = jclass("org.telegram.messenger.NotificationCenter")
    JAVA_CLASSES_FOUND = True
except Exception as e:
    log(f"Translator [FATAL]: Failed to import core Java classes: {e}")
    JAVA_CLASSES_FOUND = False

__name__ = "TranslatorPlus"
__description__ = "Ultra-fast multi-provider translation for Telegram"
__icon__ = "luvztroyicons/1"
__id__ = "translatorplus"
__version__ = "1.3.0"
__author__ = "@xwvux"
__min_version__ = "11.12.0"
