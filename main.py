import json
import os
import random
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

ADMIN_USERNAME = "abyoungg"

DATA_DIR = Path(__file__).parent / "data"
REG_FILE = DATA_DIR / "registrations.json"
HEROES_FILE = DATA_DIR / "heroes.json"

# Вероятности редких режимов при запуске /start_game
# PROB_ALL_SPIES — вероятность режима "все шпионы"
# PROB_ALL_NON_SPIES — вероятность режима "никто не шпион"
# Оба заданы как дроби от 0 до 1
PROB_ALL_SPIES = 0.10
PROB_ALL_NON_SPIES = 0.10


def load_heroes() -> List[Dict[str, str]]:
    if not HEROES_FILE.exists():
        raise RuntimeError("heroes.json не найден. Сначала скачайте список героев.")
    try:
        data = json.loads(HEROES_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("heroes.json поврежден.") from exc
    if not isinstance(data, list):
        raise RuntimeError("heroes.json имеет неверный формат.")
    return data


HEROES: List[Dict[str, str]] = []
registrations: Dict[str, Dict[str, str]] = {}
active_games: Dict[int, Dict[str, object]] = {}
lobbies: Dict[int, Dict[str, object]] = {}
ITEMS: Dict[str, Dict[str, object]] = {}
SKILLS: Dict[str, List[Dict[str, object]]] = {}


def load_registrations() -> Dict[str, Dict[str, str]]:
    if not REG_FILE.exists():
        return {}
    try:
        data = json.loads(REG_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if isinstance(data, list):
        return {str(user_id): {} for user_id in data}
    if isinstance(data, dict):
        return {str(k): v for k, v in data.items()}
    return {}


def load_items() -> Dict[str, Dict[str, object]]:
    items_file = DATA_DIR / "items.json"
    if not items_file.exists():
        return {}
    try:
        data = json.loads(items_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if isinstance(data, list):
        # convert to id -> item mapping if list
        return {item.get("id", str(i)): item for i, item in enumerate(data)}
    if isinstance(data, dict):
        return {str(k): v for k, v in data.items()}
    return {}


def load_skills() -> Dict[str, List[Dict[str, object]]]:
    skills_file = DATA_DIR / "skills.json"
    if not skills_file.exists():
        return {}
    try:
        data = json.loads(skills_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    # expect mapping hero_name -> [skills]
    if isinstance(data, dict):
        return {str(k): v for k, v in data.items()}
    return {}


def save_registrations(data: Dict[str, Dict[str, str]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def display_name(user_id: int, chat_user: Optional[object]) -> str:
    if chat_user is not None:
        name_parts = [getattr(chat_user, "first_name", "") or "", getattr(chat_user, "last_name", "") or ""]
        name = " ".join(part for part in name_parts if part).strip()
        if name:
            return name
        username = getattr(chat_user, "username", "") or ""
        if username:
            return f"@{username}"
    reg = registrations.get(str(user_id), {})
    reg_name = " ".join(
        part for part in [reg.get("first_name", ""), reg.get("last_name", "")] if part
    ).strip()
    if reg_name:
        return reg_name
    if reg.get("username"):
        return f"@{reg['username']}"
    return f"id:{user_id}"


def username_lookup(players_info: Dict[int, Dict[str, str]], username: str) -> Optional[int]:
    norm = username.lower().lstrip("@")
    for uid, info in players_info.items():
        if info.get("username", "").lower() == norm:
            return uid
    return None


def is_admin(user: object) -> bool:
    return bool(user and getattr(user, "username", "") and user.username.lower() == ADMIN_USERNAME.lower())


def build_vote_keyboard(chat_id: int, players: List[int], players_info: Dict[int, Dict[str, str]]) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for player_id in players:
        info = players_info.get(player_id, {})
        name = display_name(player_id, type("U", (), info))
        rows.append([InlineKeyboardButton(name, callback_data=f"vote:{chat_id}:{player_id}")])
    return InlineKeyboardMarkup(rows)


def build_restart_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Начать новую игру", callback_data=f"game:restart:{chat_id}")]]
    )


def build_lobby_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Старт", callback_data=f"lobby:join:{chat_id}")],
            [InlineKeyboardButton("Готово", callback_data=f"lobby:ready:{chat_id}")],
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    if chat.type != "private":
        await update.message.reply_text("Напиши мне в личку /start для регистрации.")
        return
    user = update.effective_user
    if user is None:
        return
    registrations[str(user.id)] = {
        "username": user.username or "",
        "first_name": user.first_name or "",
        "last_name": user.last_name or "",
    }
    save_registrations(registrations)
    await update.message.reply_text(
        "Вы успешно зарегистрированы!\n\n"
        "Теперь:\n"
        "1. Добавь меня в групповой чат\n"
        "2. Попроси других участников написать мне /start в личку\n"
        "3. В группе используй /start_game для начала игры"
    )


def _eligible_status(status: str) -> bool:
    return status in {"member", "administrator", "creator"}


async def start_game_core(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    reply: Callable[..., object],
    players: Optional[List[int]] = None,
    players_info: Optional[Dict[int, Dict[str, str]]] = None,
    mode: str = "default",
) -> None:
    if chat_id in active_games:
        await reply("Игра уже идет. Используй /end_game чтобы завершить.")
        return

    if players is None or players_info is None:
        players = []
        players_info = {}
        for user_id in registrations.keys():
            try:
                member = await context.bot.get_chat_member(chat_id, int(user_id))
            except Exception:
                continue
            if _eligible_status(member.status):
                players.append(int(user_id))
                user = member.user
                players_info[int(user_id)] = {
                    "first_name": user.first_name or "",
                    "last_name": user.last_name or "",
                    "username": user.username or "",
                }

    if len(players) < 3:
        await reply(
            "Недостаточно игроков. Нужно минимум 3 зарегистрированных участника, которые есть в этом чате."
        )
        return

    if not HEROES:
        await reply("Список героев пуст. Проверьте data/heroes.json.")
        return

    random.shuffle(players)
    hero = random.choice(HEROES)

    # determine spies depending on mode
    if mode == "all_spies":
        spy_ids = set(players)
    elif mode == "all_non_spies":
        spy_ids = set()
    else:
        spy_ids = {random.choice(players)}

    active_games[chat_id] = {
        "players": players,
        "spy_ids": spy_ids,
        "hero": hero,
        "mode": mode,
        "votes": {},
        "players_info": players_info,
    }

    failed_dms = 0
    for idx, player_id in enumerate(players, start=1):
        try:
            if player_id in spy_ids:
                # spy message (do not reveal internal mode)
                text = (
                    "Вы - ШПИОН!\n\n"
                    "Ваша задача:\n"
                    "- Угадать, о каком герое говорят другие игроки\n"
                    "- Подстроиться под их описания\n"
                    "- Не выдать себя\n\n"
                    f"Ваш номер в очереди: {idx}\n\n"
                    "Игра проходит в группе. Слушайте внимательно описания других игроков."
                )
                await context.bot.send_message(player_id, text)
                continue

            text = (
                f"Ваша роль: {hero['name']}\n"
                f"{hero['desc']}\n\n"
                "Ваша задача:\n"
                "- Описывать характеристики этого героя\n"
                "- Не называть героя напрямую\n"
                "- Вычислить шпиона среди игроков\n\n"
                f"Ваш номер в очереди: {idx}\n\n"
                "Игра проходит в группе. Ожидайте своей очереди для описания роли!"
            )

            # attach skills and items info if available
            hero_skills = []
            hero_items = []
            if isinstance(hero, dict):
                hero_skills = hero.get("skills", []) or []
                hero_items = hero.get("default_items", []) or []

            # fallback to global SKILLS mapping; SKILLS entry may be a list or dict {skills:[], default_items:[]}
            sk_entry = None
            try:
                sk_entry = SKILLS.get(hero.get("name")) if isinstance(hero, dict) else SKILLS.get(str(hero))
            except Exception:
                sk_entry = None
            if sk_entry:
                if isinstance(sk_entry, dict):
                    hero_skills = hero_skills or sk_entry.get("skills", []) or []
                    hero_items = hero_items or sk_entry.get("default_items", []) or []
                elif isinstance(sk_entry, list):
                    hero_skills = hero_skills or sk_entry

            if hero_skills:
                skills_lines = ["\nСкиллы:\n"]
                for sk in hero_skills:
                    sk_name = sk.get("name") if isinstance(sk, dict) else str(sk)
                    sk_desc = sk.get("desc", "") if isinstance(sk, dict) else ""
                    skills_lines.append(f"- {sk_name}: {sk_desc}")
                text += "\n" + "\n".join(skills_lines)

            if hero_items:
                items_lines = ["\nСтартовые предметы:\n"]
                for it_id in hero_items:
                    it = ITEMS.get(it_id) or {}
                    items_lines.append(f"- {it.get('name', it_id)}: {it.get('desc', '')}")
                text += "\n" + "\n".join(items_lines)

            image_path = hero.get("image", "")
            photo_path = Path(__file__).parent / image_path if image_path else None
            if photo_path and photo_path.exists():
                with photo_path.open("rb") as photo:
                    await context.bot.send_photo(player_id, photo=photo, caption=text)
            else:
                await context.bot.send_message(player_id, text)
        except Exception:
            failed_dms += 1

    order_lines = []
    for idx, player_id in enumerate(players, start=1):
        info = players_info.get(player_id, {})
        name = display_name(player_id, type("U", (), info))
        order_lines.append(f"{idx}. {name}")

    rules_text = (
        "Правила:\n"
        "- Игроки отвечают по порядку (см. список выше)\n"
        "- Описывайте свою роль, не называя героя напрямую\n"
        "- После обсуждения используйте /vote @username для голосования\n"
        "- Используйте /end_game чтобы завершить игру и раскрыть роли"
    )

    await reply(
        "Игра началась!\n\n"
        "Порядок ответов:\n"
        + "\n".join(order_lines)
        + "\n\nРоли отправлены в личные сообщения!\n\n"
        + rules_text
        + "\n\nГолосование: нажмите кнопку с именем игрока"
        + (f"\n\nНе удалось отправить ролей: {failed_dms}" if failed_dms else ""),
        reply_markup=build_vote_keyboard(chat_id, players, players_info),
    )


async def start_game(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    if chat.type not in {"group", "supergroup"}:
        await update.message.reply_text("Эта команда работает только в группах.")
        return
    if update.message is None:
        return
    # случайный выбор режима: редко - все шпионы или никто не шпион
    r = random.random()
    # вероятности задаются константами PROB_ALL_SPIES и PROB_ALL_NON_SPIES
    if r < PROB_ALL_SPIES:
        mode = "all_spies"
    elif r < PROB_ALL_SPIES + PROB_ALL_NON_SPIES:
        mode = "all_non_spies"
    else:
        mode = "default"
    await open_lobby(chat.id, context, update.message.reply_text, mode=mode)


async def start_game_all_spies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    if chat.type not in {"group", "supergroup"}:
        await update.message.reply_text("Эта команда работает только в группах.")
        return
    if update.message is None:
        return
    # open lobby in special mode -> store mode in lobby
    lobbies[chat.id] = {"players": set(), "players_info": {}, "message_id": None, "mode": "all_spies"}
    msg = await update.message.reply_text(
        "Набор в игру открыт!\n"
        "1. Нажми Старт, чтобы войти в игру\n"
        "2. Когда все готовы, нажмите Готово\n\n"
        "Учитываются только те, кто нажал Старт в этой группе",
        reply_markup=build_lobby_keyboard(chat.id),
    )
    if msg is not None and hasattr(msg, "message_id"):
        lobbies[chat.id]["message_id"] = msg.message_id


async def start_game_all_non_spies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    if chat.type not in {"group", "supergroup"}:
        await update.message.reply_text("Эта команда работает только в группах.")
        return
    if update.message is None:
        return
    lobbies[chat.id] = {"players": set(), "players_info": {}, "message_id": None, "mode": "all_non_spies"}
    msg = await update.message.reply_text(
        "Набор в игру открыт!\n"
        "1. Нажми Старт, чтобы войти в игру\n"
        "2. Когда все готовы, нажмите Готово\n\n"
        "Учитываются только те, кто нажал Старт в этой группе",
        reply_markup=build_lobby_keyboard(chat.id),
    )
    if msg is not None and hasattr(msg, "message_id"):
        lobbies[chat.id]["message_id"] = msg.message_id


async def open_lobby(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    reply: Callable[..., object],
    mode: str = "default",
) -> None:
    if chat_id in active_games:
        await reply("Игра уже идет. Используй /end_game чтобы завершить.")
        return
    lobbies[chat_id] = {"players": set(), "players_info": {}, "message_id": None, "mode": mode}
    # режим хранится в lobbies[chat_id]["mode"], но не показывается в чате
    msg = await reply(
        "Набор в игру открыт!\n"
        "1. Нажми Старт, чтобы войти в игру\n"
        "2. Когда все готовы, нажмите Готово\n\n"
        "Учитываются только те, кто нажал Старт в этой группе",
        reply_markup=build_lobby_keyboard(chat.id),
    )
    if msg is not None and hasattr(msg, "message_id"):
        lobbies[chat_id]["message_id"] = msg.message_id


async def lobby_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        return
    data = query.data or ""
    if not data.startswith("lobby:"):
        await query.answer()
        return
    parts = data.split(":", 2)
    if len(parts) != 3:
        await query.answer("Неверные данные.", show_alert=True)
        return
    action = parts[1]
    try:
        chat_id = int(parts[2])
    except ValueError:
        await query.answer("Неверные данные.", show_alert=True)
        return

    lobby = lobbies.get(chat_id)
    if lobby is None:
        await query.answer("Лобби не найдено.", show_alert=True)
        return

    user = query.from_user
    if action == "join":
        try:
            member = await context.bot.get_chat_member(chat_id, user.id)
        except Exception:
            await query.answer("Не удалось проверить участника.", show_alert=True)
            return
        if not _eligible_status(member.status):
            await query.answer("Нужно быть участником чата.", show_alert=True)
            return
        if str(user.id) not in registrations:
            await query.answer("Сначала напиши боту /start в личку.", show_alert=True)
            return
        players: Set[int] = lobby["players"]
        players_info: Dict[int, Dict[str, str]] = lobby["players_info"]
        players.add(user.id)
        players_info[user.id] = {
            "first_name": user.first_name or "",
            "last_name": user.last_name or "",
            "username": user.username or "",
        }
        names = [display_name(pid, type("U", (), players_info.get(pid, {}))) for pid in players]
        text = (
            "Набор в игру открыт!\n"
            "1. Нажми Старт, чтобы войти в игру\n"
            "2. Когда все готовы, нажмите Готово\n\n"
            "Учитываются только те, кто нажал Старт в этой группе\n\n"
            "Участники: " + (", ".join(names) if names else "пока никто")
        )
        try:
            await query.edit_message_text(text, reply_markup=build_lobby_keyboard(chat_id))
        except BadRequest:
            pass
        await query.answer("Добавлен в игру")
        return

    if action == "ready":
        players: Set[int] = lobby.get("players", set())
        players_info: Dict[int, Dict[str, str]] = lobby.get("players_info", {})
        if len(players) < 3:
            await query.answer("Нужно минимум 3 игрока.", show_alert=True)
            return
        # стартуем игру с выбранными
        mode = lobby.get("mode", "default")
        lobbies.pop(chat_id, None)
        await start_game_core(chat_id, context, query.message.reply_text, list(players), players_info, mode=mode)
        await query.answer()
        return

    await query.answer()


async def vote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    if chat.type not in {"group", "supergroup"}:
        await update.message.reply_text("Эта команда работает только в группах.")
        return
    game = active_games.get(chat.id)
    if not game:
        await update.message.reply_text("Сейчас нет активной игры.")
        return
    voter = update.effective_user
    if voter is None or voter.id not in game["players"]:
        await update.message.reply_text("Голосовать могут только участники текущей игры.")
        return

    target_id: Optional[int] = None
    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
        if target_user:
            target_id = target_user.id
    if target_id is None and context.args:
        target_id = username_lookup(game["players_info"], context.args[0])

    if target_id is None or target_id not in game["players"]:
        await update.message.reply_text("Укажи игрока: /vote @username или ответом на сообщение.")
        return

    votes: Dict[int, int] = game["votes"]
    votes[voter.id] = target_id
    await update.message.reply_text("Голос принят.")


def format_active_games() -> str:
    if not active_games:
        return "Активных игр нет."
    lines: List[str] = ["Активные игры:"]
    for chat_id, game in active_games.items():
        hero = game["hero"]["name"]
        spy_ids = game.get("spy_ids", set())
        players = game["players"]
        players_info = game["players_info"]
        mode = game.get("mode", "default")
        lines.append(f"Чат {chat_id}: игроков {len(players)}, роль: {hero}, режим: {mode}")
        for player_id in players:
            info = players_info.get(player_id, {})
            name = display_name(player_id, type("U", (), info))
            role = "ШПИОН" if player_id in spy_ids else hero
            lines.append(f"- {name}: {role}")
    return "\n".join(lines)


async def vote_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    data = query.data or ""
    if not data.startswith("vote:"):
        await query.answer()
        return
    parts = data.split(":", 2)
    if len(parts) != 3:
        await query.answer("Неверные данные.", show_alert=True)
        return
    try:
        chat_id = int(parts[1])
        target_id = int(parts[2])
    except ValueError:
        await query.answer("Неверные данные.", show_alert=True)
        return
    game = active_games.get(chat_id)
    if not game:
        await query.answer("Игра не найдена.", show_alert=True)
        return
    voter = query.from_user
    if voter.id not in game["players"]:
        await query.answer("Голосовать могут только участники игры.", show_alert=True)
        return
    if target_id not in game["players"]:
        await query.answer("Игрок не найден.", show_alert=True)
        return
    votes: Dict[int, int] = game["votes"]
    votes[voter.id] = target_id
    await query.answer("Голос принят.")


async def game_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        return
    data = query.data or ""
    if not data.startswith("game:restart:"):
        await query.answer()
        return
    try:
        chat_id = int(data.split(":", 2)[2])
    except ValueError:
        await query.answer("Неверные данные.", show_alert=True)
        return
    await open_lobby(chat_id, context, query.message.reply_text)
    await query.answer()


async def admin_games(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user):
        await update.message.reply_text("Команда доступна только администратору.")
        return

    keyboard = [
        [InlineKeyboardButton("Обновить список", callback_data="admin:list")],
        [InlineKeyboardButton("Завершить все игры", callback_data="admin:end_all")],
    ]
    for chat_id in active_games.keys():
        keyboard.append([InlineKeyboardButton(f"Завершить чат {chat_id}", callback_data=f"admin:end:{chat_id}")])
    await update.message.reply_text(
        format_active_games(),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    user = query.from_user
    if not is_admin(user):
        await query.answer("Недостаточно прав.", show_alert=True)
        return
    data = query.data or ""
    if data == "admin:list":
        try:
            await query.edit_message_text(
                format_active_games(),
                reply_markup=query.message.reply_markup if query.message else None,
            )
        except BadRequest:
            await query.answer("Нет изменений.")
            return
        await query.answer()
        return
    if data == "admin:end_all":
        ended = list(active_games.keys())
        active_games.clear()
        text = "Активных игр нет." if not ended else f"Завершены игры в чатах: {', '.join(map(str, ended))}"
        await query.edit_message_text(text)
        await query.answer()
        return
    if data.startswith("admin:end:"):
        try:
            chat_id = int(data.split(":", 2)[2])
        except ValueError:
            await query.answer("Неверный chat_id.", show_alert=True)
            return
        if chat_id not in active_games:
            await query.answer("Такой активной игры нет.", show_alert=True)
            return
        active_games.pop(chat_id, None)
        await query.edit_message_text(f"Игра в чате {chat_id} завершена.")
        await query.answer()
        return
    await query.answer()


async def end_game(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    if chat.type not in {"group", "supergroup"}:
        await update.message.reply_text("Эта команда работает только в группах.")
        return
    game = active_games.pop(chat.id, None)
    if not game:
        await update.message.reply_text("Сейчас нет активной игры.")
        return

    players: List[int] = game["players"]
    spy_ids: Set[int] = game.get("spy_ids", set())
    hero: Dict[str, str] = game["hero"]
    players_info: Dict[int, Dict[str, str]] = game["players_info"]
    votes: Dict[int, int] = game["votes"]

    roles_lines = []
    for player_id in players:
        info = players_info.get(player_id, {})
        name = display_name(player_id, type("U", (), info))
        role = "ШПИОН" if player_id in spy_ids else hero["name"]
        roles_lines.append(f"{name} - {role}")

    votes_lines = []
    if votes:
        tally: Dict[int, int] = {}
        for target_id in votes.values():
            tally[target_id] = tally.get(target_id, 0) + 1
        for target_id, count in sorted(tally.items(), key=lambda x: (-x[1], x[0])):
            info = players_info.get(target_id, {})
            name = display_name(target_id, type("U", (), info))
            votes_lines.append(f"{name}: {count}")
    else:
        votes_lines.append("Голосов не было")

    result_text = "Результат неопределенный (не было голосов)"
    if votes:
        counts: Dict[int, int] = {}
        for target_id in votes.values():
            counts[target_id] = counts.get(target_id, 0) + 1
        top = sorted(counts.items(), key=lambda x: (-x[1], x[0]))
        if len(top) > 1 and top[0][1] == top[1][1]:
            result_text = "Результат неопределенный (несколько лидеров)"
        else:
            top_id = top[0][0]
            # win if any of the revealed top players is a spy
            if top_id in spy_ids:
                result_text = "Шпион найден!"
            else:
                result_text = "Шпион не найден."

    await update.message.reply_text(
        "Результаты игры:\n\n"
        f"Роль была: {hero['name']}\n\n"
        "Роли игроков:\n"
        + "\n".join(roles_lines)
        + "\n\nГолосование:\n"
        + "\n".join(votes_lines)
        + "\n\n"
        + result_text,
        reply_markup=build_restart_keyboard(chat.id),
    )


def main() -> None:
    env_path = Path(__file__).parent / ".env"
    load_dotenv(dotenv_path=env_path, override=True)
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN не задан в переменной окружения. Добавь его в .env или окружение.")

    global registrations, HEROES, ITEMS, SKILLS
    registrations = load_registrations()
    HEROES = load_heroes()
    ITEMS = load_items()
    SKILLS = load_skills()

    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("start_game", start_game))
    app.add_handler(CommandHandler("start_game_all_spies", start_game_all_spies))
    app.add_handler(CommandHandler("start_game_all_non_spies", start_game_all_non_spies))
    app.add_handler(CommandHandler("vote", vote))
    app.add_handler(CallbackQueryHandler(vote_callback, pattern=r"^vote:"))
    app.add_handler(CallbackQueryHandler(game_callback, pattern=r"^game:restart:"))
    app.add_handler(CallbackQueryHandler(lobby_callback, pattern=r"^lobby:"))
    app.add_handler(CommandHandler("admin_games", admin_games))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^admin:"))
    app.add_handler(CommandHandler("end_game", end_game))

    app.run_polling()


if __name__ == "__main__":
    main()
