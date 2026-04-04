import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramForbiddenError
import aiosqlite

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
            await db.execute("ALTER TABLE users ADD COLUMN username TEXT")
        except Exception:
            pass # Если колонка уже есть, идем дальше
            
        # Таблица для запросов
        await db.execute("""CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            department TEXT,
            type TEXT,
            datetime TEXT
        )""")
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
@dp.message(Command("trainees"), F.chat.type == "private")
async def cmd_trainees(message: types.Message):
    if not await is_admin(message.from_user.id): return
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, department, stage, username FROM users WHERE role = 'trainee' AND is_active = 1") as c:
            rows = await c.fetchall()
            
    if not rows:
        return await message.answer("Активные стажеры отсутствуют.")
        
    lines = ["<b>Список стажеров:</b>\n"]
    for uid, dept, stage, username in rows:
        display_name = username if username else "Имя не загружено (/update)"
        lines.append(f"👤 <a href='tg://user?id={uid}'>{display_name}</a>\nДепартамент: {dept} | Этап: {stage}\n")
        
    text = "\n".join(lines)
    await message.answer(text[:4096], parse_mode="HTML")

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
    await call.message.edit_text("Укажите желаемую дату и время слота (например: 15.05.2026 18:00):")
    await state.set_state(RequestEvent.waiting_for_datetime)
    
@dp.message(RequestEvent.waiting_for_datetime)
async def process_req_datetime(message: types.Message, state: FSMContext):
    dt = message.text
    data = await state.get_data()
    etype = 'interview' if data['stage'] == 'Интервью' else 'training'
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO requests (user_id, department, type, datetime) VALUES (?, ?, ?, ?)",
                         (message.from_user.id, data['department'], etype, dt))
        await db.commit()
        
    await message.answer("Ваш запрос успешно отправлен администраторам!")
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
    b.button(text="Удалить запрос 🗑", callback_data=f"delreq_{rid}")
    b.button(text="Назад к департаментам", callback_data="backreqs")
    b.adjust(1)
    
    await call.message.edit_text(text, reply_markup=b.as_markup(), parse_mode="HTML")

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
        types.BotCommand(command="profile", description="Мой профиль")
        types.BotCommand(command="request", description="Запросить слот"),
    ]
    await bot.set_my_commands(main_menu_commands)

async def main():
    await init_db()
    await set_main_menu(bot)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
