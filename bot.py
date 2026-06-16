import asyncio
import logging
import os
from pathlib import Path
from typing import Optional
from aiogram import html
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramForbiddenError
from aiogram.fsm.storage.base import StorageKey
import aiosqlite
ITEMS_PER_PAGE = 7

# === НАСТРОЙКИ ===
BOT_TOKEN = os.getenv("BOT_TOKEN", "8622961253:AAEkR6VSv3WnLKjNJ19eJkPjmM9dfLz5jB8") # Вставь свой токен сюда
CREATOR_ID = 7616343249 # Вставь свой ID сюда
DB_PATH = os.getenv("DATABASE_PATH", "data/airline_bot.db")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)

# === СОСТОЯНИЯ (FSM) ===
class AddTrainee(StatesGroup):
    waiting_for_dept = State()

class ChangeDept(StatesGroup):
    waiting_for_dept = State()

class ExamMessage(StatesGroup):
    waiting_for_text = State()

class CreateEvent(StatesGroup):
    choosing_type = State()
    choosing_dept = State()
    waiting_for_date = State()
    waiting_for_time = State()
    waiting_for_location = State()
    waiting_for_description = State()
    confirming = State()

class RequestEvent(StatesGroup):
    waiting_for_dept = State()
    waiting_for_datetime = State()

class NotifyEvent(StatesGroup):
    waiting_for_dept = State()
    waiting_for_type = State()

class EditNotify(StatesGroup):
    waiting_for_text = State()

class TicketEvent(StatesGroup):
    waiting_for_question = State()
    waiting_for_answer = State()
    waiting_for_reply = State()

class RequestAccept(StatesGroup):
    waiting_for_discord = State()

class ExamEvent(StatesGroup):
    waiting_for_id = State()

# === БАЗА ДАННЫХ ===
async def init_db():
    db_file = Path(DB_PATH)
    db_file.parent.mkdir(parents=True, exist_ok=True)
    
    async with aiosqlite.connect(DB_PATH) as db:
        # Роли: 'head_admin', 'admin', 'trainee'
        await db.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            role TEXT,
            department TEXT,
            stage TEXT,
            is_active INTEGER DEFAULT 1
        )""")
        
        # Типы событий: 'interview', 'training'
        await db.execute("""CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT,
            department TEXT,
            date TEXT,
            time TEXT,
            location TEXT,
            description TEXT,
            host_name TEXT
        )""")

        await db.execute("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )""")
        # Установим стандартное сообщение, если его еще нет
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('notify_template', 'Появился новый слот!\nДепартамент: {dept}\nТип: {type}')")
        
        await db.execute("""CREATE TABLE IF NOT EXISTS bookings (
            event_id INTEGER,
            user_id INTEGER,
            PRIMARY KEY(event_id, user_id)
        )""")
        
        await db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        
        # Добавляем базовый текст экзамена, если его нет
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('exam_text', 'Ссылка на экзамен пока не задана.')")
        # Пытаемся добавить колонку username, если её еще нет
        try:
            await db.execute("ALTER TABLE tickets ADD COLUMN admin_id INTEGER")
        except Exception:
            pass
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('interview_accept_msg', 'Ваш запрос на собеседование принят! Присоединитесь к нашему стаф-порталу (https://discord.gg/e459Y7GrNX) и напишите ваш ник в Discord для связи:')")
            
        # Таблица для запросов
        await db.execute("""CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            department TEXT,
            type TEXT,
            datetime TEXT
        )""")

        await db.execute("""CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            question TEXT,
            status TEXT DEFAULT 'open'
        )""")

        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('pass_msg', 'С радостью сообщаем, что Вы успешно прошли все три этапа отборочного процесса. Отдел Кадров высоко оценил Ваш уровень компетенций и опыт, которые в полной мере соответствуют нашим требованиям и ожиданиям. Мы были впечатлены Вашими результатами на каждом из этапов. Для дальнейших инструкций обратитесь @antoninaiivanovna')")
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('fail_msg', 'Информируем вас, что по результатам экзамена ваша кандидатура не была утверждена департаментом кадров. К сожалению, текущий результат не соответствует установленным требованиям для данной позиции. Вы можете повторно направить заявку на участие в следующем отборочном туре.')")
        # Статус запросов: 1 - включены, 0 - выключены
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('requests_enabled', '1')")
        
        await db.commit()

# === ПРОВЕРКИ ПРАВ ===
async def get_user_data(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT role, department, stage, is_active FROM users WHERE user_id = ?", (user_id,)) as c:
            return await c.fetchone()

async def is_creator(user_id: int) -> bool:
    return user_id == CREATOR_ID

async def is_head_admin(user_id: int) -> bool:
    if await is_creator(user_id): return True
    data = await get_user_data(user_id)
    return data is not None and data[0] == 'head_admin' and data[3] == 1

async def is_admin(user_id: int) -> bool:
    if await is_head_admin(user_id): return True
    data = await get_user_data(user_id)
    return data is not None and data[0] in ('admin', 'head_admin') and data[3] == 1

async def is_active_trainee(user_id: int) -> bool:
    data = await get_user_data(user_id)
    return data is not None and data[0] == 'trainee' and data[3] == 1

# === КЛАВИАТУРЫ ===
def get_departments_kb(prefix: str):
    b = InlineKeyboardBuilder()
    b.button(text="Пилоты", callback_data=f"{prefix}_pilots")
    b.button(text="Наземные службы", callback_data=f"{prefix}_ground")
    b.button(text="Бортпроводники", callback_data=f"{prefix}_cabin")
    b.adjust(1)
    return b.as_markup()

def get_pagination_kb(items: list, page: int, per_page: int, prefix: str):
    b = InlineKeyboardBuilder()
    start = page * per_page
    
    for item_id, item_text in items[start:start+per_page]:
        b.button(text=str(item_text), callback_data=f"{prefix}_select_{item_id}")
        
    b.adjust(1)
    total = max(1, (len(items) - 1) // per_page + 1)
    
    nav = []
    nav.append(InlineKeyboardButton(text="|<|" if page > 0 else " ", callback_data=f"{prefix}_page_{page-1}" if page > 0 else "ignore"))
    nav.append(InlineKeyboardButton(text=f"|{page+1}/{total}|", callback_data="ignore"))
    nav.append(InlineKeyboardButton(text="|>|" if page < total - 1 else " ", callback_data=f"{prefix}_page_{page+1}" if page < total - 1 else "ignore"))
    b.row(*nav)
    
    return b.as_markup()

def get_cancel_skip_kb(allow_skip: bool = True):
    b = InlineKeyboardBuilder()
    if allow_skip: 
        b.button(text="Пропустить ⏭", callback_data="create_skip")
    b.button(text="Отменить ❌", callback_data="create_cancel")
    b.adjust(1)
    return b.as_markup()

# === СИСТЕМА УПРАВЛЕНИЯ ПЕРСОНАЛОМ (ГЛАВНЫЕ АДМИНЫ И СОЗДАТЕЛЬ) ===
@dp.message(Command("add_head"), F.chat.type == "private")
async def cmd_add_head(message: types.Message):
    if not await is_creator(message.from_user.id): return
    args = message.text.split()
    if len(args) != 2 or not args[1].isdigit():
        return await message.answer("Формат: /add_head <ID пользователя>")
    uid = int(args[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO users (user_id, role, is_active) VALUES (?, 'head_admin', 1)", (uid,))
        await db.commit()
    await message.answer("Пользователь назначен главным администратором.")

@dp.message(Command("add_admin"), F.chat.type == "private")
async def cmd_add_admin(message: types.Message):
    if not await is_head_admin(message.from_user.id): return
    args = message.text.split()
    if len(args) != 2 or not args[1].isdigit():
        return await message.answer("Формат: /add_admin <ID пользователя>")
    uid = int(args[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO users (user_id, role, is_active) VALUES (?, 'admin', 1)", (uid,))
        await db.commit()
    await message.answer("Пользователь назначен администратором.")

@dp.message(Command("add_trainee"), F.chat.type == "private")
async def cmd_add_trainee(message: types.Message, state: FSMContext):
    if not await is_head_admin(message.from_user.id): return
    args = message.text.split()
    if len(args) != 2 or not args[1].isdigit():
        return await message.answer("Формат: /add_trainee <ID пользователя>")
    
    uid = int(args[1])
    await state.update_data(target_id=uid)
    await message.answer("Укажите департамент для стажера:", reply_markup=get_departments_kb("dept"))
    await state.set_state(AddTrainee.waiting_for_dept)

@dp.callback_query(F.data.startswith("dept_"), AddTrainee.waiting_for_dept)
async def process_add_trainee_dept(call: CallbackQuery, state: FSMContext):
    dept_map = {"dept_pilots": "Пилоты", "dept_ground": "Наземные службы", "dept_cabin": "Бортпроводники"}
    dept = dept_map.get(call.data)
    data = await state.get_data()
    uid = data['target_id']
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO users (user_id, role, department, stage, is_active) VALUES (?, 'trainee', ?, 'Интервью', 1)", (uid, dept))
        await db.commit()
        
    await call.message.edit_text("Стажер успешно добавлен. Текущий этап: Интервью.")
    await state.clear()

@dp.message(Command("change_dept"), F.chat.type == "private")
async def cmd_change_dept(message: types.Message, state: FSMContext):
    if not await is_head_admin(message.from_user.id): return
    args = message.text.split()
    if len(args) != 2 or not args[1].isdigit():
        return await message.answer("Формат: /change_dept <ID пользователя>")
    
    await state.update_data(target_id=int(args[1]))
    await message.answer("Укажите новый департамент:", reply_markup=get_departments_kb("cdept"))
    await state.set_state(ChangeDept.waiting_for_dept)

@dp.callback_query(F.data.startswith("cdept_"), ChangeDept.waiting_for_dept)
async def process_change_dept(call: CallbackQuery, state: FSMContext):
    dept_map = {"cdept_pilots": "Пилоты", "cdept_ground": "Наземные службы", "cdept_cabin": "Бортпроводники"}
    dept = dept_map.get(call.data)
    data = await state.get_data()
    uid = data['target_id']
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET department = ? WHERE user_id = ?", (dept, uid))
        await db.commit()
        
    await call.message.edit_text("Департамент успешно изменен.")
    await state.clear()

@dp.message(Command("advance"), F.chat.type == "private")
async def cmd_advance(message: types.Message):
    if not await is_head_admin(message.from_user.id): return
    args = message.text.split()
    if len(args) != 2 or not args[1].isdigit():
        return await message.answer("Формат: /advance <ID пользователя>")
        
    uid = int(args[1])
    data = await get_user_data(uid)
    if not data or data[0] != 'trainee':
        return await message.answer("Пользователь не найден или не является стажером.")
        
    current_stage = data[2]
    next_stage = ""
    if current_stage == "Интервью": next_stage = "Тренинг"
    elif current_stage == "Тренинг": next_stage = "Экзамен"
    elif current_stage == "Экзамен": next_stage = "Завершено"
    else: return await message.answer("Стажер уже завершил обучение.")
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET stage = ? WHERE user_id = ?", (next_stage, uid))
        await db.commit()
        
    await message.answer(f"Статус стажера обновлен. Текущий этап: {next_stage}.")

@dp.message(Command("kick"), F.chat.type == "private")
async def cmd_kick(message: types.Message):
    if not await is_head_admin(message.from_user.id): return
    args = message.text.split()
    if len(args) != 2 or not args[1].isdigit():
        return await message.answer("Формат: /kick <ID пользователя>")
        
    uid = int(args[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_active = 0 WHERE user_id = ?", (uid,))
        await db.commit()
        
    await message.answer("Пользователь отстранен от обучения. Доступ к системе закрыт.")

# === ЭКЗАМЕН (ГЛАВНЫЕ АДМИНЫ) ===
@dp.message(Command("edit_exam"), F.chat.type == "private")
async def cmd_edit_exam(message: types.Message, state: FSMContext):
    if not await is_head_admin(message.from_user.id): return
    await message.answer("Введите текст сообщения для экзамена (включая ссылки):")
    await state.set_state(ExamMessage.waiting_for_text)

@dp.message(ExamMessage.waiting_for_text)
async def process_exam_text(message: types.Message, state: FSMContext):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE settings SET value = ? WHERE key = 'exam_text'", (message.text,))
        await db.commit()
    await message.answer("Текст экзамена сохранен.")
    await state.clear()

@dp.message(Command("send_exam"), F.chat.type == "private")
async def cmd_send_exam(message: types.Message):
    if not await is_head_admin(message.from_user.id): return
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key = 'exam_text'") as c:
            text_row = await c.fetchone()
        async with db.execute("SELECT user_id FROM users WHERE stage = 'Экзамен' AND is_active = 1") as c:
            trainees = await c.fetchall()
            
    exam_text = text_row[0] if text_row else "Текст экзамена не установлен."
    count = 0
    
    for (uid,) in trainees:
        try:
            await bot.send_message(uid, f"Уведомление об экзамене.\n\n{exam_text}")
            count += 1
        except TelegramForbiddenError:
            pass
            
    await message.answer(f"Рассылка завершена. Доставлено стажерам: {count}.")

# === ПРОСМОТР СТАЖЕРОВ (ДЛЯ ВСЕХ АДМИНОВ) ===
async def send_trainees_page(event, page: int):
    # Определяем, вызвано ли это через команду (Message) или через кнопку (CallbackQuery)
    is_callback = isinstance(event, types.CallbackQuery)
    message = event.message if is_callback else event
    
    async with aiosqlite.connect(DB_PATH) as db:
        # 1. Считаем общее количество активных стажеров в реальном времени
        async with db.execute("SELECT COUNT(*) FROM users WHERE role = 'trainee' AND is_active = 1") as c:
            total_count = (await c.fetchone())[0]
            
    # Если стажеров вообще нет в базе
    if total_count == 0:
        text = "📋 Список стажеров пуст."
        if is_callback:
            await event.message.edit_text(text, reply_markup=None)
            await event.answer()
        else:
            await message.answer(text)
        return

    # 2. Высчитываем общее количество страниц
    total_pages = math.ceil(total_count / ITEMS_PER_PAGE)
    
    # Защита от выхода за границы (например, если стажеров удалили, пока админ листал)
    if page < 1: page = 1
    if page > total_pages: page = total_pages

    # Высчитываем сдвиг для SQL-запроса
    offset = (page - 1) * ITEMS_PER_PAGE

    # 3. Достаем из базы только нужную "порцию" стажеров для текущей страницы
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, username, first_name, department, stage FROM users WHERE role = 'trainee' AND is_active = 1 LIMIT ? OFFSET ?",
            (ITEMS_PER_PAGE, offset)
        ) as c:
            trainees = await c.fetchall()

    # Сводка в заголовке
    text = f"📋 <b>Активные стажеры (Всего: {total_count}):</b>\n\n"
    
    for uid, username, first_name, dept, stage in trainees:
        display_name = username if username else first_name
        safe_name = html.quote(display_name)
        text += f"👤 <a href='tg://user?id={uid}'>{safe_name}</a> (<code>{uid}</code>)\nДепартамент: {dept} | Этап: {stage}\n\n"

    # 4. Создаем кнопки навигации
    kb = InlineKeyboardBuilder()
    
    # Кнопка "Назад" (влево)
    if page > 1:
        kb.button(text="⬅️ Назад", callback_data=f"traineespage_{page-1}")
    else:
        kb.button(text="⏹️", callback_data="ignore_click")
        
    # Центральная кнопка с индикатором страниц (просто текст, клик ничего не делает)
    kb.button(text=f"Стр. {page}/{total_pages}", callback_data="ignore_click")
    
    # Кнопка "Вперед" (вправо)
    if page < total_pages:
        kb.button(text="Вперед ➡️", callback_data=f"traineespage_{page+1}")
    else:
        kb.button(text="⏹️", callback_data="ignore_click")
        
    kb.adjust(3) # Выстраиваем кнопки строго в один ряд из 3 штук

    # 5. Отправляем пользователю
    if is_callback:
        try:
            # Обновляем старое сообщение, чтобы интерфейс не прыгал
            await event.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
        except Exception:
            # На случай, если админ нажал на кнопку страницы, на которой уже находится
            pass
        await event.answer()
    else:
        # Если ввели команду текстом, шлем новое сообщение
        await message.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


# --- ОБРАБОТЧИКИ КОМАНДЫ И КНОПОК ---

# Сама команда /trainees (всегда открывает первую страницу)
@dp.message(Command("trainees"), F.chat.type == "private")
async def cmd_trainees(message: types.Message):
    if not await is_admin(message.from_user.id): return
    await send_trainees_page(message, page=1)
    # Обработка нажатий на стрелочки
@dp.callback_query(F.data.startswith("traineespage_"))
async def process_trainees_page(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id): 
        return await call.answer("У вас нет прав.", show_alert=True)
    
    # Достаем номер страницы из callback_data
    page = int(call.data.split("_")[1])
    await send_trainees_page(call, page)

# Заглушка для некликабельных кнопок (индикатор и пустые стрелки)
@dp.callback_query(F.data == "ignore_click")
async def process_ignore_click(call: types.CallbackQuery):
    await call.answer()

@dp.message(Command("toggle_requests"), F.chat.type == "private")
async def cmd_toggle_requests(message: types.Message):
    if not await is_admin(message.from_user.id): return
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key = 'requests_enabled'") as c:
            row = await c.fetchone()
            
        current_state = row[0] if row else '1'
        new_state = '0' if current_state == '1' else '1'
        
        await db.execute("UPDATE settings SET value = ? WHERE key = 'requests_enabled'", (new_state,))
        await db.commit()
        
    status_text = "✅ ВКЛЮЧЕНЫ" if new_state == '1' else "❌ ВЫКЛЮЧЕНЫ"
    await message.answer(f"Запросы на тренинги и собеседования теперь {status_text}.")


@dp.message(Command("update"), F.chat.type == "private")
async def cmd_update(message: types.Message):
    if not await is_admin(message.from_user.id): return
    await message.answer("Начинаю обновление никнеймов стажеров. Это может занять несколько секунд...")
    
    updated = 0
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users WHERE role = 'trainee' AND is_active = 1") as c:
            users = await c.fetchall()
            
        for (uid,) in users:
            try:
                chat = await bot.get_chat(uid)
                name = f"@{chat.username}" if chat.username else chat.first_name
                await db.execute("UPDATE users SET username = ? WHERE user_id = ?", (name, uid))
                updated += 1
            except Exception:
                pass # Если пользователь заблокировал бота, пропускаем
        await db.commit()
        
    await message.answer(f"Обновление завершено! Обновлено профилей: {updated}.")


@dp.message(CommandStart(), F.chat.type == "private")
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    
    # Если зашел создатель
    if await is_creator(user_id):
        return await message.answer(
            "Привет, Создатель! 👑\n"
            "Твои команды:\n"
            "/add_head <ID> - назначить главного админа\n"
            "/create - создать слот"
        )
        
    # Проверяем, есть ли человек в базе
    data = await get_user_data(user_id)
    if not data or data[3] == 0:
        return await message.answer("Доступ закрыт. Вы не числитесь в системе авиакомпании.")
        
    role, dept, stage, active = data
    
    if role == 'trainee':
        await message.answer(
            f"Добро пожаловать в систему, стажер!\n"
            f"Департамент: {dept}\n"
            f"Ваш этап: {stage}\n\n"
            "Используйте /profile, /interview или /training"
        )
    else:
        await message.answer("Добро пожаловать в панель управления персоналом. Введите /create для планирования.")

# === СОЗДАНИЕ МЕРОПРИЯТИЙ (ДЛЯ ВСЕХ АДМИНОВ) ===
@dp.message(Command("create"), F.chat.type == "private")
async def cmd_create(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id): return
    
    b = InlineKeyboardBuilder()
    b.button(text="Интервью", callback_data="ctype_interview")
    b.button(text="Тренинг", callback_data="ctype_training")
    b.adjust(1)
    
    await message.answer("Выберите тип слота для создания:", reply_markup=b.as_markup())
    await state.set_state(CreateEvent.choosing_type)

@dp.callback_query(F.data.startswith("ctype_"), CreateEvent.choosing_type)
async def process_create_type(call: CallbackQuery, state: FSMContext):
    event_type = call.data.split("_")[1]
    await state.update_data(type=event_type)
    
    if event_type == "interview":
        await call.message.edit_text("Ввод данных. Укажите дату:", reply_markup=get_cancel_skip_kb(False))
        await state.set_state(CreateEvent.waiting_for_date)
    else:
        await call.message.edit_text("Укажите департамент для тренинга:", reply_markup=get_departments_kb("tdept"))
        await state.set_state(CreateEvent.choosing_dept)

@dp.callback_query(F.data.startswith("tdept_"), CreateEvent.choosing_dept)
async def process_create_dept(call: CallbackQuery, state: FSMContext):
    dept_map = {"tdept_pilots": "Пилоты", "tdept_ground": "Наземные службы", "tdept_cabin": "Бортпроводники"}
    await state.update_data(department=dept_map.get(call.data))
    await call.message.edit_text("Ввод данных. Укажите дату:", reply_markup=get_cancel_skip_kb(False))
    await state.set_state(CreateEvent.waiting_for_date)

@dp.callback_query(F.data == "create_cancel", StateFilter(CreateEvent))
async def cancel_creation(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Действие отменено.")

@dp.callback_query(F.data == "create_skip", StateFilter(CreateEvent))
async def skip_creation_step(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await process_creation_step(call.message, state, is_skip=True, user=call.from_user)

@dp.message(StateFilter(CreateEvent.waiting_for_date, CreateEvent.waiting_for_time, 
                        CreateEvent.waiting_for_location, CreateEvent.waiting_for_description))
async def process_creation_step(message: types.Message, state: FSMContext, is_skip=False, user: types.User = None):
    user_obj = user if is_skip else message.from_user
    if not await is_admin(user_obj.id): return
    
    st = await state.get_state()
    val = " " if is_skip else message.text
    data = await state.get_data()

    if st == CreateEvent.waiting_for_date.state:
        await state.update_data(date=val)
        await state.set_state(CreateEvent.waiting_for_time)
        await message.answer("Укажите время:", reply_markup=get_cancel_skip_kb(False))
        
    elif st == CreateEvent.waiting_for_time.state:
        await state.update_data(time=val)
        if data['type'] == 'interview':
            await finalize_creation(message, state, user_obj)
        else:
            await state.set_state(CreateEvent.waiting_for_location)
            await message.answer("Укажите место проведения:", reply_markup=get_cancel_skip_kb(False))
            
    elif st == CreateEvent.waiting_for_location.state:
        await state.update_data(location=val)
        await state.set_state(CreateEvent.waiting_for_description)
        await message.answer("Укажите описание:", reply_markup=get_cancel_skip_kb(False))
        
    elif st == CreateEvent.waiting_for_description.state:
        await state.update_data(description=val)
        await finalize_creation(message, state, user_obj)

async def finalize_creation(message: types.Message, state: FSMContext, user_obj: types.User):
    data = await state.get_data()
    host = f"@{user_obj.username}" if user_obj.username else user_obj.first_name
    await state.update_data(host=host)
    
    preview = f"Тип: {'Интервью' if data['type'] == 'interview' else 'Тренинг'}\n\n"
    if data['type'] == 'training':
        preview += f"Департамент: {data.get('department')}\n\n"
        
    preview += f"Дата: {data.get('date')}\n\nВремя: {data.get('time')}\n\n"
    
    if data['type'] == 'training':
        preview += f"Место: {data.get('location', ' ')}\n\nОписание: {data.get('description', ' ')}\n\n"
        
    b = InlineKeyboardBuilder()
    b.button(text="Подтвердить ✅", callback_data="confirm_event")
    b.button(text="Отменить ❌", callback_data="create_cancel")
    
    await message.answer(f"Предпросмотр данных:\n\n{preview}", reply_markup=b.as_markup())
    await state.set_state(CreateEvent.confirming)

@dp.callback_query(F.data == "confirm_event", CreateEvent.confirming)
async def confirm_event(call: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""INSERT INTO events (type, department, date, time, location, description, host_name) 
                            VALUES (?, ?, ?, ?, ?, ?, ?)""",
                         (d['type'], d.get('department'), d.get('date'), d.get('time'), 
                          d.get('location', ' '), d.get('description', ' '), d['host']))
        await db.commit()
    await call.message.edit_text("Слот успешно создан.")
    await state.clear()

# === СИСТЕМА СТАЖЕРОВ (ВЗАИМОДЕЙСТВИЕ) ===
@dp.message(Command("profile"), F.chat.type == "private")
async def cmd_profile(message: types.Message):
    data = await get_user_data(message.from_user.id)
    if not data or data[3] == 0:
        return # Игнорируем неавторизованных
        
    role, dept, stage, active = data
    if role != 'trainee':
        return await message.answer("Профиль доступен только для стажеров.")
        
    text = (f"Профиль стажера\n\n"
            f"Имя: {message.from_user.first_name}\n"
            f"Департамент: {dept}\n"
            f"Текущий этап: {stage}")
    await message.answer(text)

@dp.message(Command("interview"), F.chat.type == "private")
async def cmd_interview(message: types.Message):
    data = await get_user_data(message.from_user.id)
    if not data or data[0] != 'trainee' or data[3] == 0: return
    if data[2] != 'Интервью':
        return await message.answer("Доступ отклонен. Ваш текущий этап не соответствует данному запросу.")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, date, time FROM events WHERE type = 'interview'") as c:
            events = await c.fetchall()
    actual_events = [e for e in events if is_event_actual(e[1], e[2])]
    if not actual_events:
        return await message.answer("Свободные слоты для интервью отсутствуют.")
    items = [(e[0], f"{e[1]} в {e[2]}") for e in actual_events]
    await message.answer("Доступные слоты для интервью:", reply_markup=get_pagination_kb(items, 0, 5, "book"))

@dp.message(Command("training"), F.chat.type == "private")
async def cmd_training(message: types.Message):
    data = await get_user_data(message.from_user.id)
    if not data or data[0] != 'trainee' or data[3] == 0: return
    if data[2] != 'Тренинг':
        return await message.answer("Доступ отклонен. Ваш текущий этап не соответствует данному запросу.")
    dept = data[1]
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, date, time FROM events WHERE type = 'training' AND department = ?", (dept,)) as c:
            events = await c.fetchall()
    actual_events = [e for e in events if is_event_actual(e[1], e[2])]
    if not actual_events:
        return await message.answer("Свободные слоты для тренингов отсутствуют.")
    items = [(e[0], f"{e[1]} в {e[2]}") for e in actual_events]
    await message.answer("Доступные слоты для тренинга:", reply_markup=get_pagination_kb(items, 0, 5, "book"))

@dp.callback_query(F.data.startswith("book_select_"))
async def select_booking_slot(call: CallbackQuery):
    uid = call.from_user.id
    if not await is_active_trainee(uid): return await call.answer("Доступ закрыт.", show_alert=True)
    
    event_id = int(call.data.split("_")[2])
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT type, date, time, location, description, host_name FROM events WHERE id = ?", (event_id,)) as c:
            event = await c.fetchone()
            
    if not event: return await call.answer("Слот не найден.", show_alert=True)
    
    etype, edate, etime, eloc, edesc, ehost = event
    
    if etype == 'training':
        text = f"Хост: {ehost}\n\nДата: {edate}\n\nВремя: {etime}\n\nДля записи нажмите на кнопку ниже."
    else:
        text = f"Хост: {ehost}\n\nДата: {edate}\n\nВремя: {etime}\n\nДля записи нажмите на кнопку ниже."
        
    b = InlineKeyboardBuilder()
    b.button(text="Записаться ✅", callback_data=f"confirmbook_{event_id}")
    await call.message.edit_text(text, reply_markup=b.as_markup())

@dp.callback_query(F.data.startswith("confirmbook_"))
async def confirm_booking(call: CallbackQuery):
    uid = call.from_user.id
    if not await is_active_trainee(uid): return await call.answer("Доступ закрыт.", show_alert=True)
    
    event_id = int(call.data.split("_")[1])
    
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("INSERT INTO bookings (event_id, user_id) VALUES (?, ?)", (event_id, uid))
            await db.commit()
            await call.message.edit_text("Спасибо за запись.")
        except aiosqlite.IntegrityError:
            await call.message.edit_text("Вы уже записаны на данный слот.")

@dp.callback_query(F.data == "ignore")
async def ignore_cb(call: CallbackQuery):
    await call.answer()

@dp.callback_query(F.data.regexp(r"^book_page_"))
async def paginate_books(call: CallbackQuery):
    page = int(call.data.split("_")[2])
    uid = call.from_user.id
    data = await get_user_data(uid)
    
    if not data or data[0] != 'trainee' or data[3] == 0: 
        return await call.answer("Доступ закрыт.", show_alert=True)
        
    dept = data[1]
    stage = data[2]
    
    async with aiosqlite.connect(DB_PATH) as db:
        if stage == 'Интервью':
            async with db.execute("SELECT id, date, time FROM events WHERE type = 'interview'") as c:
                events = await c.fetchall()
        elif stage == 'Тренинг':
            async with db.execute("SELECT id, date, time FROM events WHERE type = 'training' AND department = ?", (dept,)) as c:
                events = await c.fetchall()
        else:
            return await call.answer("Нет доступных мероприятий.", show_alert=True)
            
    actual_events = [e for e in events if is_event_actual(e[1], e[2])]
    items = [(e[0], f"{e[1]} в {e[2]}") for e in actual_events]
    
    if not items:
        return await call.answer("Доступных слотов больше нет.", show_alert=True)
        
    await call.message.edit_reply_markup(reply_markup=get_pagination_kb(items, page, 5, "book"))

from datetime import datetime

@dp.message(Command("my_bookings"), F.chat.type == "private")
async def cmd_my_bookings(message: types.Message):
    uid = message.from_user.id
    if not await is_active_trainee(uid): return
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""SELECT e.id, e.type, e.date, e.time 
                                 FROM events e JOIN bookings b ON e.id = b.event_id 
                                 WHERE b.user_id = ?""", (uid,)) as c:
            bookings = await c.fetchall()
            
    if not bookings:
        return await message.answer("У вас нет активных записей.")
        
    for eid, etype, edate, etime in bookings:
        name = "Интервью" if etype == "interview" else "Тренинг"
        b = InlineKeyboardBuilder()
        b.button(text="Отменить запись ❌", callback_data=f"cancelbook_{eid}")
        await message.answer(f"{name} | {edate} в {etime}", reply_markup=b.as_markup())

@dp.callback_query(F.data.startswith("cancelbook_"))
async def process_cancel_booking(call: CallbackQuery):
    eid = int(call.data.split("_")[1])
    uid = call.from_user.id
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM bookings WHERE event_id = ? AND user_id = ?", (eid, uid))
        await db.commit()
        
    await call.message.edit_text("Вы успешно отменили свою запись на это мероприятие.")

@dp.message(Command("my_events"), F.chat.type == "private")
async def cmd_my_events(message: types.Message):
    if not await is_admin(message.from_user.id): return
    host = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, type, date, time FROM events WHERE host_name = ?", (host,)) as c:
            events = await c.fetchall()
            
    if not events:
        return await message.answer("У вас нет созданных мероприятий.")
        
    for eid, etype, edate, etime in events:
        name = "Интервью" if etype == "interview" else "Тренинг"
        b = InlineKeyboardBuilder()
        b.button(text="Кто записался? 👥", callback_data=f"viewevent_{eid}")
        b.button(text="Удалить слот 🗑", callback_data=f"delevent_{eid}")
        b.adjust(1)
        await message.answer(f"Слот: {name} | {edate} в {etime}", reply_markup=b.as_markup())

@dp.callback_query(F.data.startswith("viewevent_"))
async def process_view_event(call: CallbackQuery):
    eid = int(call.data.split("_")[1])
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM bookings WHERE event_id = ?", (eid,)) as c:
            users = await c.fetchall()
            
    if not users:
        return await call.answer("Пока никто не записался.", show_alert=True)
        
    text = "Список записавшихся (ID):\n" + "\n".join([str(u[0]) for u in users])
    await call.message.answer(text)
    await call.answer()

@dp.callback_query(F.data.startswith("delevent_"))
async def process_delete_event(call: CallbackQuery):
    eid = int(call.data.split("_")[1])
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM events WHERE id = ?", (eid,))
        await db.execute("DELETE FROM bookings WHERE event_id = ?", (eid,))
        await db.commit()
        
    await call.message.edit_text("Слот успешно удален, все записи на него аннулированы.")

def is_event_actual(date_str: str, time_str: str) -> bool:
    try:
        event_dt = datetime.strptime(f"{date_str} {time_str}", "%d.%m.%Y %H:%M")
        return event_dt > datetime.now()
    except ValueError:
        return True 

# Эти две функции нужно заменить в твоем коде, чтобы работало скрытие по времени
@dp.message(Command("interview"), F.chat.type == "private")
async def cmd_interview(message: types.Message):
    data = await get_user_data(message.from_user.id)
    if not data or data[0] != 'trainee' or data[3] == 0: return
    if data[2] != 'Интервью':
        return await message.answer("Доступ отклонен. Ваш текущий этап не соответствует данному запросу.")
        
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, date, time FROM events WHERE type = 'interview'") as c:
            events = await c.fetchall()
            
    actual_events = [e for e in events if is_event_actual(e[1], e[2])]
            
    if not actual_events:
        return await message.answer("Свободные слоты для интервью отсутствуют.")
        
    items = [(e[0], f"{e[1]} в {e[2]}") for e in actual_events]
    await message.answer("Доступные слоты для интервью:", reply_markup=get_pagination_kb(items, 0, 5, "book"))

# --- ЗАПРОСЫ ОТ СТАЖЕРОВ ---
@dp.message(Command("request"), F.chat.type == "private")
async def cmd_request(message: types.Message, state: FSMContext):
    # Сначала проверяем, включены ли запросы
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key = 'requests_enabled'") as c:
            row = await c.fetchone()
            if row and row[0] == '0':
                return await message.answer(" В данный момент запросы на тренинги и собеседования временно недоступны. Пожалуйста, попробуйте позже.")

    # Если включены, продолжаем стандартную логику
    data = await get_user_data(message.from_user.id)
    if not data or data[0] != 'trainee' or data[3] == 0: return
    
    stage = data[2]
    if stage not in ['Интервью', 'Тренинг']:
        return await message.answer("Ваш текущий этап обучения не позволяет создавать запросы.")
        
    await state.update_data(stage=stage)
    await message.answer("Выберите ваш департамент:", reply_markup=get_departments_kb("reqdept"))
    await state.set_state(RequestEvent.waiting_for_dept)


@dp.callback_query(F.data.startswith("reqdept_"), RequestEvent.waiting_for_dept)
async def process_req_dept(call: CallbackQuery, state: FSMContext):
    dept_map = {"reqdept_pilots": "Пилоты", "reqdept_ground": "Наземные службы", "reqdept_cabin": "Бортпроводники"}
    await state.update_data(department=dept_map.get(call.data))
    await call.message.edit_text("Укажите удобное вам время (10:00 - 20:00, время в MSK (GTM+3)")
    await state.set_state(RequestEvent.waiting_for_datetime)
    
@dp.message(RequestEvent.waiting_for_datetime)
async def process_req_datetime(message: types.Message, state: FSMContext):
    dt = message.text
    data = await state.get_data()
    etype = 'interview' if data['stage'] == 'Интервью' else 'training'
    etype_rus = "Собеседование" if etype == 'interview' else "Тренинг"
    
    # Получаем ник для уведомления создателя
    username = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO requests (user_id, department, type, datetime) VALUES (?, ?, ?, ?)",
                         (message.from_user.id, data['department'], etype, dt))
        await db.commit()
        
    await message.answer("Ваш запрос успешно отправлен!")
    
    # Уведомление создателю (замени CREATOR_ID на свой ID)
    creator_msg = f"Новый запрос!\nДеп: {data['department']}\nТип: {etype_rus}\nОт: {username}"
    try:
        await bot.send_message(CREATOR_ID, creator_msg)
    except Exception:
        pass
        
    await state.clear()


# --- УПРАВЛЕНИЕ УВЕДОМЛЕНИЯМИ ---

@dp.message(Command("edit_notify"), F.chat.type == "private")
async def cmd_edit_notify(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id): return
    await message.answer("Введите новый текст уведомления. Используйте {dept} и {type} для автоматической подстановки параметров:")
    await state.set_state(EditNotify.waiting_for_text)

@dp.message(EditNotify.waiting_for_text)
async def process_edit_notify_text(message: types.Message, state: FSMContext):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE settings SET value = ? WHERE key = 'notify_template'", (message.text,))
        await db.commit()
    await message.answer("Шаблон уведомления успешно обновлен!")
    await state.clear()

@dp.message(Command("notify"), F.chat.type == "private")
async def cmd_notify(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id): return
    await message.answer("Для какого департамента новый слот?", reply_markup=get_departments_kb("notifydept"))
    await state.set_state(NotifyEvent.waiting_for_dept)

@dp.callback_query(F.data.startswith("notifydept_"), NotifyEvent.waiting_for_dept)
async def process_notify_dept(call: CallbackQuery, state: FSMContext):
    dept_map = {"notifydept_pilots": "Пилоты", "notifydept_ground": "Наземные службы", "notifydept_cabin": "Бортпроводники"}
    await state.update_data(dept=dept_map.get(call.data))
    
    kb = InlineKeyboardBuilder()
    kb.button(text="Тренинг", callback_data="notifytype_training")
    kb.button(text="Собеседование", callback_data="notifytype_interview")
    kb.adjust(2)
    
    await call.message.edit_text("Выберите тип слота:", reply_markup=kb.as_markup())
    await state.set_state(NotifyEvent.waiting_for_type)

@dp.callback_query(F.data.startswith("notifytype_"), NotifyEvent.waiting_for_type)
async def process_notify_finish(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    dept_name = data['dept']
    # Сопоставляем тип для поиска по этапу в базе
    if call.data == "notifytype_training":
        type_name = "Тренинг"
        stage_search = "Тренинг"
    else:
        type_name = "Собеседование"
        stage_search = "Интервью"
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key = 'notify_template'") as c:
            template = await c.fetchone()
        
        # Берем только стажеров конкретного департамента на конкретном этапе
        async with db.execute(
            "SELECT user_id FROM users WHERE role = 'trainee' AND is_active = 1 AND department = ? AND stage = ?", 
            (dept_name, stage_search)
        ) as c:
            trainees = await c.fetchall()
            
    if not template:
        return await call.message.edit_text("Ошибка: шаблон не найден.")
            
    final_text = template[0].format(dept=dept_name, type=type_name)
    await call.message.edit_text(f"Рассылка для {dept_name} ({type_name})...")
    
    count = 0
    for (uid,) in trainees:
        try:
            await bot.send_message(uid, final_text)
            count += 1
        except Exception:
            pass
            
    await call.message.answer(f"Готово! Уведомлено {count} чел. из департамента {dept_name}.")
    await state.clear()


# --- ИТОГИ ЭКЗАМЕНА ---
@dp.message(Command("exam"), F.chat.type == "private")
async def cmd_exam(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id): return
    await message.answer("Введите ID стажера, чтобы вынести решение по экзамену (скопируйте из /trainees):")
    await state.set_state(ExamEvent.waiting_for_id)

@dp.message(ExamEvent.waiting_for_id)
async def process_exam_id(message: types.Message, state: FSMContext):
    uid = message.text.strip()
    if not uid.isdigit(): 
        return await message.answer("Ошибка: ID должен состоять только из цифр.")
        
    b = InlineKeyboardBuilder()
    b.button(text="✅ Сдал", callback_data=f"examres_pass_{uid}")
    b.button(text="❌ Не сдал", callback_data=f"examres_fail_{uid}")
    b.adjust(2)
    await message.answer(f"Выберите итог для стажера {uid}:", reply_markup=b.as_markup())
    await state.clear()

@dp.callback_query(F.data.startswith("examres_"))
async def process_exam_result(call: CallbackQuery):
    _, res, uid = call.data.split("_")
    uid = int(uid)
    
    key = 'pass_msg' if res == 'pass' else 'fail_msg'
    new_role = 'passed' if res == 'pass' else 'failed'
    status_text = "СДАЛ" if res == 'pass' else "НЕ СДАЛ"
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(f"SELECT value FROM settings WHERE key = '{key}'") as c:
            template = await c.fetchone()
            msg_text = template[0] if template else ("Итоги экзамена подведены.")
            
        # Отключаем стажера (is_active = 0) и меняем роль, чтобы он пропал из списков
        await db.execute("UPDATE users SET is_active = 0, role = ? WHERE user_id = ?", (new_role, uid))
        await db.commit()
        
    try:
        await bot.send_message(uid, msg_text)
        await call.message.edit_text(f"✅ Стажер {uid} получил статус {status_text}.\nИнструкция отправлена, профиль деактивирован.")
    except Exception:
        await call.message.edit_text(f"⚠️ Статус {status_text} установлен, но стажер заблокировал бота, сообщение не доставлено.")


# --- СИСТЕМА ТИКЕТОВ ---
@dp.message(Command("support"), F.chat.type == "private")
async def cmd_support(message: types.Message, state: FSMContext):
    data = await get_user_data(message.from_user.id)
    if not data or data[3] == 0: return # Только для активных
    await message.answer("Опишите вашу проблему или вопрос. Администраторы ответят вам в ближайшее время:")
    await state.set_state(TicketEvent.waiting_for_question)

@dp.message(TicketEvent.waiting_for_question)
async def process_ticket_question(message: types.Message, state: FSMContext):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO tickets (user_id, question) VALUES (?, ?)", (message.from_user.id, message.text))
        await db.commit()
    await message.answer("✅ Ваш тикет создан! Ожидайте ответа.")
    
    # Уведомляем тебя (не забудь, что CREATOR_ID должен быть прописан вверху файла)
    try:
        username = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
        await bot.send_message(CREATOR_ID, f"🆘 Новый тикет!\nОт: {username} ({message.from_user.id})\nВопрос: {message.text}\nВведите /tickets чтобы ответить.")
    except Exception:
        pass
    await state.clear()

@dp.message(Command("tickets"), F.chat.type == "private")
async def cmd_tickets(message: types.Message):
    if not await is_admin(message.from_user.id): return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, user_id, question FROM tickets WHERE status = 'open'") as c:
            tickets = await c.fetchall()
            
    if not tickets: return await message.answer("Открытых тикетов нет 🎉")
    
    for tid, uid, q in tickets:
        b = InlineKeyboardBuilder()
        b.button(text="Ответить ✍️", callback_data=f"answerticket_{tid}_{uid}")
        b.button(text="Закрыть ❌", callback_data=f"closeticket_{tid}")
        b.adjust(2)
        await message.answer(f"🎫 Тикет #{tid}\nID стажера: {uid}\nВопрос: {q}", reply_markup=b.as_markup())

@dp.callback_query(F.data.startswith("answerticket_"))
async def answer_ticket(call: CallbackQuery, state: FSMContext):
    _, tid, uid = call.data.split("_")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE tickets SET admin_id = ?, status = 'in_progress' WHERE id = ?", (call.from_user.id, tid))
        await db.commit()
    await state.update_data(ticket_id=tid, user_id=uid)
    await call.message.edit_text(f"Введите ваш ответ стажеру (Тикет #{tid}):")
    await state.set_state(TicketEvent.waiting_for_answer)

@dp.message(TicketEvent.waiting_for_answer)
async def process_ticket_answer(message: types.Message, state: FSMContext):
    data = await state.get_data()
    tid, uid = data['ticket_id'], data['user_id']
    b = InlineKeyboardBuilder()
    b.button(text="Ответить 💬", callback_data=f"replyticket_{tid}")
    try:
        await bot.send_message(uid, f"👨‍💻 Ответ администратора (Тикет #{tid}):\n\n{message.text}", reply_markup=b.as_markup())
        await message.answer(f"Ответ отправлен. Тикет #{tid} остается открытым.")
    except Exception:
        await message.answer("Ошибка отправки, стажер заблокировал бота.")
    await state.clear()

@dp.callback_query(F.data.startswith("replyticket_"))
async def reply_ticket_user(call: CallbackQuery, state: FSMContext):
    tid = call.data.split("_")[1]
    await state.update_data(ticket_id=tid)
    await call.message.edit_text("Введите ваше сообщение для администратора:")
    await state.set_state(TicketEvent.waiting_for_reply)

@dp.message(TicketEvent.waiting_for_reply)
async def process_ticket_reply(message: types.Message, state: FSMContext):
    data = await state.get_data()
    tid = data['ticket_id']
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT admin_id FROM tickets WHERE id = ?", (tid,)) as c:
            row = await c.fetchone()
            
    if not row or not row[0]: return await message.answer("Ошибка поиска администратора.")
    admin_id = row[0]
    
    b = InlineKeyboardBuilder()
    b.button(text="Ответить ✍️", callback_data=f"answerticket_{tid}_{message.from_user.id}")
    b.button(text="Закрыть ❌", callback_data=f"closeticket_{tid}")
    username = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
    
    try:
        await bot.send_message(admin_id, f"💬 Сообщение от {username} (Тикет #{tid}):\n\n{message.text}", reply_markup=b.as_markup())
        await message.answer("Отправлено!")
    except Exception:
        pass
    await state.clear()

@dp.callback_query(F.data.startswith("closeticket_"))
async def close_ticket(call: types.CallbackQuery, state: FSMContext):
    tid = call.data.split("_")[1]
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE tickets SET status = 'closed' WHERE id = ?", (tid,))
        await db.commit()
        
    await call.message.edit_text(f"✅ Тикет #{tid} успешно закрыт.")
    await call.answer("Тикет закрыт!")
    await state.clear()


# --- ПРОСМОТР ЗАПРОСОВ ДЛЯ АДМИНОВ ---
@dp.message(Command("requests"), F.chat.type == "private")
async def cmd_requests_admin(message: types.Message):
    if not await is_admin(message.from_user.id): return
    await message.answer("Выберите департамент для просмотра запросов:", reply_markup=get_departments_kb("viewreq"))

@dp.callback_query(F.data.startswith("viewreq_"))
async def view_requests_dept(call: CallbackQuery):
    dept_map = {"viewreq_pilots": "Пилоты", "viewreq_ground": "Наземные службы", "viewreq_cabin": "Бортпроводники"}
    dept = dept_map.get(call.data)
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, type, datetime FROM requests WHERE department = ?", (dept,)) as c:
            reqs = await c.fetchall()
            
    if not reqs:
        return await call.message.edit_text(f"В департаменте '{dept}' активных запросов нет.")
        
    b = InlineKeyboardBuilder()
    for rid, rtype, rdt in reqs:
        t_name = "Интервью" if rtype == "interview" else "Тренинг"
        b.button(text=f"{t_name} | {rdt}", callback_data=f"reqinfo_{rid}")
    b.adjust(1)
    await call.message.edit_text(f"Запросы ({dept}):", reply_markup=b.as_markup())

@dp.callback_query(F.data.startswith("reqinfo_"))
async def view_request_info(call: CallbackQuery):
    rid = int(call.data.split("_")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""SELECT r.type, r.datetime, u.username, u.user_id 
                                 FROM requests r LEFT JOIN users u ON r.user_id = u.user_id 
                                 WHERE r.id = ?""", (rid,)) as c:
            req = await c.fetchone()
            
    if not req: return await call.answer("Запрос не найден.", show_alert=True)
    rtype, rdt, username, uid = req
    t_name = "Интервью" if rtype == "interview" else "Тренинг"
    name_display = username if username else "Неизвестный (/update)"
    
    text = f"<b>Запрос на:</b> {t_name}\n<b>Дата и время:</b> {rdt}\n<b>Запросил:</b> 👤 <a href='tg://user?id={uid}'>{name_display}</a>"
    b = InlineKeyboardBuilder()
    b.button(text="Принять ✅", callback_data=f"acceptreq_{rid}_{uid}")
    b.button(text="Удалить запрос 🗑", callback_data=f"delreq_{rid}")
    b.button(text="Назад", callback_data="backreqs")
    b.adjust(1)
    
    await call.message.edit_text(text, reply_markup=b.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("acceptreq_"))
async def accept_request(call: CallbackQuery, state: FSMContext):
    _, rid, uid = call.data.split("_")
    uid = int(uid)
    admin_id = call.from_user.id
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT type, department FROM requests WHERE id = ?", (rid,)) as c:
            req = await c.fetchone()
        if not req: return await call.answer("Запрос уже обработан.", show_alert=True)
        rtype, dept = req
        await db.execute("DELETE FROM requests WHERE id = ?", (rid,))
        
        async with db.execute("SELECT value FROM settings WHERE key = 'interview_accept_msg'") as c:
            int_msg_row = await c.fetchone()
            interview_msg = int_msg_row[0] if int_msg_row else "Ваш запрос на собеседование принят! Напишите ваш ник в Discord:"
        await db.commit()

    user_key = StorageKey(bot_id=bot.id, chat_id=uid, user_id=uid)
    await state.storage.set_state(user_key, RequestAccept.waiting_for_discord)
    await state.storage.set_data(user_key, {'accept_admin_id': admin_id, 'req_dept': dept, 'req_type': rtype})
    
    msg_text = interview_msg if rtype == "interview" else "Ваш запрос на тренинг принят! Пожалуйста, напишите ваш ник в Discord:"
    try:
        await bot.send_message(uid, msg_text)
        await call.message.edit_text("Запрос принят. Ожидаем дискорд стажера.")
    except Exception:
        await call.message.edit_text("Не удалось связаться со стажером.")

@dp.message(RequestAccept.waiting_for_discord)
async def process_discord_nick(message: types.Message, state: FSMContext):
    data = await state.get_data()
    admin_id = data.get('accept_admin_id')
    rtype_rus = "Собеседование" if data.get('req_type') == "interview" else "Тренинг"
    dept = data.get('req_dept')
    username = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
    
    if admin_id:
        try:
            await bot.send_message(admin_id, f"✅ Стажер {username} прислал Discord для слота ({dept} | {rtype_rus}):\n\n<b>{message.text}</b>", parse_mode="HTML")
        except Exception:
            pass
    await message.answer("Ваш Discord успешно отправлен проверяющему!")
    await state.clear()

@dp.callback_query(F.data.startswith("delreq_"))
async def delete_request(call: CallbackQuery):
    rid = int(call.data.split("_")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM requests WHERE id = ?", (rid,))
        await db.commit()
    await call.message.edit_text("Запрос успешно удален.")
    
@dp.callback_query(F.data == "backreqs")
async def back_to_requests(call: CallbackQuery):
    await call.message.edit_text("Выберите департамент для просмотра запросов:", reply_markup=get_departments_kb("viewreq"))

@dp.message(Command("training"), F.chat.type == "private")
async def cmd_training(message: types.Message):
    data = await get_user_data(message.from_user.id)
    if not data or data[0] != 'trainee' or data[3] == 0: return
    if data[2] != 'Тренинг':
        return await message.answer("Доступ отклонен. Ваш текущий этап не соответствует данному запросу.")
        
    dept = data[1]
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, date, time FROM events WHERE type = 'training' AND department = ?", (dept,)) as c:
            events = await c.fetchall()
            
    actual_events = [e for e in events if is_event_actual(e[1], e[2])]
            
    if not actual_events:
        return await message.answer("Свободные слоты для тренингов отсутствуют.")
        
    items = [(e[0], f"{e[1]} в {e[2]}") for e in actual_events]
    await message.answer("Доступные слоты для тренинга:", reply_markup=get_pagination_kb(items, 0, 5, "book"))
  
async def set_main_menu(bot: Bot):
    main_menu_commands = [
        types.BotCommand(command="training", description="Доступные тренинги"),
        types.BotCommand(command="interview", description="Запись на интервью"),
        types.BotCommand(command="request", description="Запросить слот"),
        types.BotCommand(command="profile", description="Мой профиль"),
        types.BotCommand(command="support", description="Поддержка")
    ]
    await bot.set_my_commands(main_menu_commands)

async def main():
    await init_db()
    await set_main_menu(bot)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
