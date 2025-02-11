#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import asyncio
import itertools
import random
import logging
import time
import hashlib

from telethon import TelegramClient, errors
from telethon.errors import (SessionPasswordNeededError, FloodWaitError,
                            UserChannelsTooMuchError)
from telethon.tl.functions.messages import AddChatUserRequest
from telethon.tl.functions.channels import (InviteToChannelRequest,
                                            JoinChannelRequest,
                                            GetParticipantsRequest)
from telethon.tl.types import (ChannelParticipantsSearch, InputPeerUser)
from telethon.network import ConnectionTcpMTProxy
from telethon.connection import ConnectionTcpFull


from aiogram import Bot, Dispatcher, executor, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage

SETTINGS_DIR = "settings"
USERNAME_DIR = "username"
API_DIR = "api"
DATABASE_DIR = "database"
SESSION_DIR = "session"

DELAY_FILE = os.path.join(SETTINGS_DIR, "delay.py")
CONFIG_FILE = os.path.join(SETTINGS_DIR, "config.py")
DEST_FILE = os.path.join(SETTINGS_DIR, "dest.py")
PROXY_FILE = os.path.join(SETTINGS_DIR, "proxy.txt")

USERNAMES_FILE = os.path.join(USERNAME_DIR, "usernames.txt")
USED_FILE = os.path.join(USERNAME_DIR, "used.txt")

API_KEYS_FILE = os.path.join(API_DIR, "api_keys.txt")

DB_FILE = os.path.join(DATABASE_DIR, "clientbot_test.db")
BOT_DB_FILE = os.path.join(DATABASE_DIR, "bot_data.db")

sys.path.append(SETTINGS_DIR)
sys.path.append(DATABASE_DIR)

from database import pickledb

from delay import (delay_after_join, delay_after_invite, delay_after_error,
                   delay_between_accounts, delay_between_clients)

from dest import dest
from config import MAX_ACCOUNTS_PER_API, BOT_TOKEN, ADMIN_ID, LOG_CHANNEL_ID

logging.basicConfig(level=logging.ERROR, format='%(asctime)s - %(levelname)s - %(message)s')

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"

def colored_print(message, color):
    print(f"{color}{message}{RESET}")

os.makedirs(SETTINGS_DIR, exist_ok=True)
os.makedirs(USERNAME_DIR, exist_ok=True)
os.makedirs(API_DIR, exist_ok=True)
os.makedirs(DATABASE_DIR, exist_ok=True)
os.makedirs(SESSION_DIR, exist_ok=True)

accounts = {}
dests = {}
accounts_on_cooldown = {}
session_names = []
last_account_switch_time = 0
inviting_paused = False

bot = Bot(token=BOT_TOKEN, parse_mode=types.ParseMode.HTML)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

bot_db = pickledb.load(BOT_DB_FILE, False)
if not bot_db.get('inviting_paused'):
    bot_db.set('inviting_paused', False)
    bot_db.dump()

def get_file_hash(filepath):
    hasher = hashlib.sha256()
    if not os.path.exists(filepath):
        return None
    with open(filepath, 'rb') as file:
        while True:
            chunk = file.read(4096)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()

async def create_telegram_account(session_name, api_id, api_hash, proxy=None):
    session_path = os.path.join(SESSION_DIR, session_name)
    try:
        if proxy:
            proxy_type, addr, port, username, password = proxy

            if proxy_type.lower() == 'mtproto':
                account = TelegramClient(session_path, api_id, api_hash,
                                         connection=ConnectionTcpMTProxy,
                                         proxy=(addr, port, username))
            else:
                account = TelegramClient(session_path, api_id, api_hash,
                                         connection=ConnectionTcpFull,
                                         proxy=(proxy_type, addr, port, username, password))
        else:
            account = TelegramClient(session_path, api_id, api_hash,
                                     connection=ConnectionTcpFull)

        await account.connect()

        if not await account.is_user_authorized():
            colored_print(f"  ⛔ Сессия {session_name} не авторизована 😔. Пропускаем...", RED)
            await send_log_to_telegram(f"❌ Сессия <b>{session_name}</b> не авторизована 😔.")
            return None

        try:
            me = await account.get_me()
            if not me.first_name:
                raise ValueError("У аккаунта отсутствует имя.")
        except ValueError as e:
            colored_print(f"  ⛔ Ошибка: {e}. Пропускаем сессию {session_name}", RED)
            await send_log_to_telegram(f"❌ Ошибка в сессии <b>{session_name}</b>: отсутствует имя 😢.")
            return None
        except Exception as e:
            colored_print(f"  ⛔ Не удалось получить данные аккаунта {session_name}: {e}. Пропускаем.", RED)
            await send_log_to_telegram(f"❌ Ошибка в сессии <b>{session_name}</b>: не удалось получить данные аккаунта.")
            return None

    except Exception as e:
        colored_print(f"  ⛔ Не удалось создать аккаунт {session_name}: {e}. Пропускаем...", RED)
        await send_log_to_telegram(f"❌ Ошибка при создании аккаунта <b>{session_name}</b>: {e}")
        return None

    return account


async def send_log_to_telegram(message):
    try:
        await bot.send_message(LOG_CHANNEL_ID, message)
    except Exception as e:
        colored_print(f"  ERROR Ошибка при отправке сообщения в лог-канал: {e} 😢", RED)
        await bot.send_message(LOG_CHANNEL_ID, message)


@dp.message_handler(commands=['start'])
async def start_bot(message: types.Message):
    if str(message.from_user.id) == ADMIN_ID:
        await message.reply("🤖 Бот-помощник активирован! ✅ Ожидаю команд... 📡")


async def main():
    global last_account_switch_time, session_names, inviting_paused, db

    colored_print("🚀🚀🚀 Запуск скрипта!  Начинаем работу! ✨✨✨", GREEN)
    await send_log_to_telegram("🚀🚀🚀 <b>Запуск скрипта!</b> Начинаем работу! ✨✨✨")

    try:
        with open(API_KEYS_FILE, 'r') as f:
            api_keys = [line.strip().split(':') for line in f if line.strip()]
    except FileNotFoundError:
        colored_print(f"  ⛔ Файл {API_KEYS_FILE} не найден 😢. Пожалуйста, добавьте файл с API ключами.", RED)
        await send_log_to_telegram(f"❌ Файл <code>{API_KEYS_FILE}</code> не найден 😢. Пожалуйста, добавьте файл с API ключами.")
        sys.exit(1)

    if not api_keys:
        colored_print(f"  ⛔ Файл {API_KEYS_FILE} пустой 🫥. Необходимо добавить API ключи.", RED)
        await send_log_to_telegram(f"❌ Файл <code>{API_KEYS_FILE}</code> пустой 🫥. Необходимо добавить API ключи.")
        sys.exit(1)

    api_key_cycle = itertools.cycle(api_keys)

    proxies = []
    if os.path.exists(PROXY_FILE):
        try:
            with open(PROXY_FILE, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            parts = line.split(':')
                            if len(parts) == 3:
                                proxy_type = 'mtproto'
                                addr, port, secret = parts
                                proxies.append((proxy_type, addr, int(port), secret, None))
                            elif len(parts) >= 2:
                                if len(parts) == 4:
                                      proxy_type, addr, port, userpass = parts
                                      username, password = userpass.split(':', 1)
                                elif len(parts) == 2:
                                    addr, port = parts
                                    proxy_type = 'http'
                                    username, password = None, None
                                elif len(parts) == 3:
                                    try:
                                        proxy_type, addr, port = parts
                                        username, password = None, None
                                    except ValueError:
                                        addr, port, userpass = parts
                                        username, password = userpass.split(':', 1)
                                        proxy_type = 'http'
                                proxies.append((proxy_type, addr, int(port), username, password))
                            else:
                                 raise ValueError(f"Неверный формат прокси в строке: {line}")
                        except ValueError as e:
                            colored_print(f"  ⚠ Ошибка при чтении прокси: {e}. Строка: {line}. Пропускаем...", YELLOW)
                            continue


        except FileNotFoundError:
            colored_print(f"  ⚠ Файл {PROXY_FILE} не найден. Работаем без прокси.", YELLOW)
            await send_log_to_telegram("⚠ Файл с прокси не найден. Работаем без прокси.")
        except Exception as e:
            colored_print(f"  ⛔ Ошибка при чтении файла {PROXY_FILE}: {e}. Продолжаем работу без прокси.", RED)
            await send_log_to_telegram(f"❌ Ошибка при чтении файла с прокси: {e}. Продолжаем работу без прокси.")
    else:
        colored_print(f"  ⚠ Файл {PROXY_FILE} не найден. Работаем без прокси.", YELLOW)
        await send_log_to_telegram("⚠ Файл с прокси не найден. Работаем без прокси.")

    proxy_cycle = itertools.cycle(proxies) if proxies else None

    session_names = [f for f in os.listdir(SESSION_DIR) if f.endswith('.session')]
    if not session_names:
        colored_print(f"  ⛔ Папка {SESSION_DIR} пустая 🫥. Пожалуйста, добавьте файлы сессий Telegram.", RED)
        await send_log_to_telegram(f"❌ Папка <code>{SESSION_DIR}</code> пустая 🫥. Пожалуйста, добавьте файлы сессий Telegram.")
        sys.exit(1)

    api_key_counts = {}
    valid_session_names = []
    for session_name in session_names:
        colored_print(f'  ✅ Пробуем авторизовать аккаунт {session_name} 💫', GREEN)
        api_id, api_hash = next(api_key_cycle)
        proxy = next(proxy_cycle) if proxy_cycle else None

        account = await create_telegram_account(session_name, api_id, api_hash, proxy)

        if account:
            try:
                dests[session_name] = await account.get_entity(dest)
                valid_session_names.append(session_name)
                accounts[session_name] = account
                colored_print(f'    🟢 Аккаунт {session_name} успешно авторизован! ✨', GREEN)
                await send_log_to_telegram(f"✅ Аккаунт <b>{session_name}</b> успешно авторизован! ✨")

                if (api_id, api_hash) not in api_key_counts:
                    api_key_counts[(api_id, api_hash)] = 0
                api_key_counts[(api_id, api_hash)] += 1
                if api_key_counts[(api_id, api_hash)] > MAX_ACCOUNTS_PER_API:
                    colored_print(f"  ⛔ Превышено максимальное количество аккаунтов ({MAX_ACCOUNTS_PER_API}) на одном ключе API {api_id}:{api_hash}. 🤯", RED)
                    await send_log_to_telegram(f"❌ Превышено максимальное количество аккаунтов на API: <b>{api_id}:{api_hash}</b>. 🤯")
                    sys.exit(1)
            except Exception as e:
                colored_print(f"  ⛔ Не удалось получить информацию о целевой группе для аккаунта {session_name}: {e}. Пропускаем.", RED)
                await send_log_to_telegram(f"❌ Ошибка в сессии <b>{session_name}</b>: Не удалось получить информацию о целевой группе 😔.")
                continue
        else:
            pass


    if not valid_session_names:
        colored_print("  ⛔ Нет активных аккаунтов 😭. Завершаем работу.", RED)
        await send_log_to_telegram("❌ Нет активных аккаунтов 😭. Завершаем работу.")
        sys.exit(1)

    session_names = valid_session_names
    colored_print('🏁 Скрипт запущен и готов к работе! 💪🎉', GREEN)
    await send_log_to_telegram("🏁 Скрипт запущен и готов к работе! 💪🎉")

    db = pickledb.load(DB_FILE, True)


    current_usernames_hash = get_file_hash(USERNAMES_FILE)
    previous_usernames_hash = db.get('usernames_hash')

    if current_usernames_hash != previous_usernames_hash:
        colored_print("  🔄 Файл со списком пользователей изменился 🔄. Начинаем с начала 😉.", GREEN)
        await send_log_to_telegram("🔄 Файл со списком пользователей изменился. Начинаем с начала 😉.")
        db.set('start', 0)
        db.set('usernames_hash', current_usernames_hash)
        ind = 0
    else:
        ind = int(db.get('start')) if db.get('start') is not None else 0
        with open(USERNAMES_FILE, 'r') as f:
            usernames_list = [line.strip() for line in f]
        start_username = usernames_list[ind] if ind < len(usernames_list) else "неизвестен 🤷"

        colored_print(f'  🔄 Продолжаем с пользователя {start_username} (номер {ind}) 🔄', GREEN)
        await send_log_to_telegram(f"🔄 Продолжаем с пользователя <b>{start_username}</b> (номер <b>{ind}</b>) 🔄")

    joined_group = {session_name: False for session_name in session_names}

    try:
        with open(USERNAMES_FILE, 'r') as f:
            usernames = [line.strip() for line in f]
    except FileNotFoundError:
        colored_print(f"  ⛔ Файл {USERNAMES_FILE} не найден 😢.", RED)
        await send_log_to_telegram(f"❌ Файл <code>{USERNAMES_FILE}</code> не найден 😢.")
        usernames = []

    if not usernames:
        while True:
            source_chat_url = input(f"  📝 Файл {USERNAMES_FILE} пустой 🫥. Введите ссылку на чат, откуда брать пользователей: ")
            try:
                if valid_session_names:
                    account = accounts[valid_session_names[0]]
                    source_chat = await account.get_entity(source_chat_url)
                    break
                else:
                    colored_print("  ⛔ Нет активных аккаунтов для получения информации о чате 😭. Завершаем работу.", RED)
                    await send_log_to_telegram("❌ Нет активных аккаунтов для получения информации о чате 😭. Завершаем работу.")
                    sys.exit(1)
            except ValueError:
                colored_print("  ⚠ Некорректная ссылка 🤕. Попробуйте ещё раз.", RED)
                await send_log_to_telegram(f"⚠ Некорректная ссылка 🤕: <code>{source_chat_url}</code>")
            except Exception as e:
                colored_print(f"  ⛔ Не удалось получить информацию о чате: {e}", RED)
                await send_log_to_telegram(f"❌ Не удалось получить информацию о чате: <code>{source_chat_url}</code>: {e}")
                break

        try:
            participants = await account(GetParticipantsRequest(
                source_chat,
                ChannelParticipantsSearch(''),
                0, 1000,
                hash=0
            ))
            usernames = [user.username for user in participants.users if user.username]
            await send_log_to_telegram(f"✅ Получили список пользователей из чата <code>{source_chat_url}</code> ({len(usernames)} человек) 🎉.  Начинаем работу! 🤝")

        except Exception as e:
            colored_print(f"  ⛔ Не удалось получить список участников чата: {e}", RED)
            await send_log_to_telegram(f"❌ Не удалось получить список участников чата: {e}")
            sys.exit(1)


    while True:
        if bot_db.get('inviting_paused'):
            await asyncio.sleep(5)
            continue

        if ind >= len(usernames):
            colored_print('  🎉🎉🎉 Все пользователи обработаны! Успешное завершение! 🎉🎉🎉', GREEN)
            await send_log_to_telegram("🎉🎉🎉 Все пользователи обработаны! Успешное завершение! 🎉🎉🎉")
            break

        username = usernames[ind]

        current_time = time.time()
        if current_time - last_account_switch_time < delay_between_clients:
            remaining_delay = delay_between_clients - (current_time - last_account_switch_time)
            colored_print(f"  ⏳ Ждём {remaining_delay:.2f} секунд перед сменой аккаунта ⏱", YELLOW)
            await asyncio.sleep(remaining_delay)

        rd_session_name = random.choice(session_names)
        colored_print(f'  🔄 Переключаемся на аккаунт {rd_session_name} ⚙️', GREEN)
        last_account_switch_time = time.time()

        account = accounts[rd_session_name]
        dest_entity = dests[rd_session_name]

        if rd_session_name in accounts_on_cooldown:
            remaining_cooldown = accounts_on_cooldown[rd_session_name] - time.time()
            if remaining_cooldown > 0:
                colored_print(f'  ⏰ Аккаунт {rd_session_name} на перерыве ещё {remaining_cooldown:.2f} секунд 🛌. Пропускаем...', RED)
                await asyncio.sleep(delay_between_accounts)

                all_on_cooldown = True
                for s_name in session_names:
                    if s_name not in accounts_on_cooldown or accounts_on_cooldown[s_name] <= time.time():
                        all_on_cooldown = False
                        break
                if all_on_cooldown:
                    colored_print("  ⚠ Все аккаунты на перерыве 😴. Завершаем работу.", RED)
                    await send_log_to_telegram("⚠ Все аккаунты на перерыве 😴. Завершаем работу.")
                    sys.exit(1)

                continue
            else:
                del accounts_on_cooldown[rd_session_name]

        try:
            user = await account.get_input_entity(username)

            if not joined_group[rd_session_name]:
                try:
                    await account(JoinChannelRequest(dest_entity))
                    colored_print(f"    ➕ Аккаунт {rd_session_name} вступил в целевую группу ✅", GREEN)
                    await send_log_to_telegram(f"➕ Аккаунт <b>{rd_session_name}</b> вступил в целевую группу ✅")
                    joined_group[rd_session_name] = True
                    await asyncio.sleep(delay_after_join)
                except errors.rpcerrorlist.ImportBotAuthorizationRequiredError:
                    colored_print(f"    ⛔ Аккаунт {rd_session_name} - бот 🤖. Боты не могут вступать в группы.", RED)
                    await send_log_to_telegram(f"⚠ Аккаунт <b>{rd_session_name}</b> - бот 🤖.")
                    joined_group[rd_session_name] = True
                    await asyncio.sleep(delay_after_error)
                    continue
                except Exception as e:
                    colored_print(f"    ⛔ Ошибка при вступлении аккаунта {rd_session_name} в целевую группу: {e}", RED)
                    await send_log_to_telegram(f"⚠ Ошибка при вступлении аккаунта <b>{rd_session_name}</b> в целевую группу: {e}")
                    await asyncio.sleep(delay_after_error)
                    continue

            if isinstance(user, InputPeerUser):
                await account(InviteToChannelRequest(dest_entity, [user]))
                colored_print(f"    ✉ {ind + 1}: Пользователю @{username} отправлено приглашение 📨", GREEN)

                participants = await account(GetParticipantsRequest(
                    dest_entity,
                    ChannelParticipantsSearch(username),
                    0, 1,
                    hash=0
                ))
                if participants.users:
                    colored_print(f"    ✅ {ind + 1}: Пользователь @{username} успешно добавлен! 🎉", GREEN)
                    await send_log_to_telegram(f"✅ [{ind+1}/{len(usernames)}] Пользователь <b>@{username}</b> успешно добавлен! 🎉 (аккаунт <b>{rd_session_name}</b>)")
                    with open(USED_FILE, 'a') as used_file:
                        used_file.write(f"{username}\n")
                else:
                    colored_print(f"    ⚠ {ind + 1}: Не удалось добавить пользователя @{username} 😔", RED)
            else:
                colored_print(f"    ⚠ {ind + 1}: Нельзя пригласить @{username}, это не пользователь 🤷 (возможно, канал, чат или бот).", RED)
                await send_log_to_telegram(f"⚠ Нельзя пригласить @{username}, это не пользователь 🤷.")
                await asyncio.sleep(delay_after_error)
                continue

        except ValueError:
            colored_print(f'      ⚠ Пользователь @{username} не найден 🤷. 