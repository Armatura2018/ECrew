import asyncio
import logging
from typing import Optional
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

# === НАСТРОЙКИ ===
BOT_TOKEN = "8373494520:AAEyyfltaGAxhnRPOkjebO5LL9GS5eG78go"
CREATOR_ID = 8134413995  # Твой ID

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)

# === БАЗА ДАННЫХ (В ПАМЯТИ ДЛЯ ТЕСТА) ===
admins = {CREATOR_ID}
known_groups = {}  # chat_id: chat_name
events_db = {}     # event_id: {group_id, name, date, time, location, description, creator_id, host_username}
active_posts = {}  # message_id (в группе): {event_id, group_id, attendees: set()}

# === СОСТОЯНИЯ (FSM) ===
class CreateEvent(StatesGroup):
    choosing_group = State()
    waiting_for_name = State()
    waiting_for_date = State()
    waiting_for_time = State()
    waiting_for_location = State()
    waiting_for_description = State()
    confirming = State()

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (Пагинация) ===
def get_pagination_keyboard(items: list, page: int, per_page: int, callback_prefix: str) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    start = page * per_page
    end = start + per_page
    
    for item_id, item_name in items[start:end]:
        builder.button(text=item_name, callback_data=f"{callback_prefix}_select_{item_id}")
    
    builder.adjust(1)
    
    total_pages = (len(items) - 1) // per_page + 1
    if total_pages > 0:
        nav_row = []
        nav_row.append(InlineKeyboardButton(
            text="|<|" if page > 0 else " ", 
            callback_data=f"{callback_prefix}_page_{page-1}" if page > 0 else "ignore"
        ))
        nav_row.append(InlineKeyboardButton(text=f"|{page+1}/{total_pages}|", callback_data="ignore"))
        nav_row.append(InlineKeyboardButton(
            text="|>|" if page < total_pages - 1 else " ", 
            callback_data=f"{callback_prefix}_page_{page+1}" if page < total_pages - 1 else "ignore"
        ))
        builder.row(*nav_row)
    return builder

# Кнопки Отмена / Пропустить
def get_cancel_skip_kb(allow_skip: bool = True):
    builder = InlineKeyboardBuilder()
    if allow_skip:
        builder.button(text="Пропустить ⏭", callback_data="create_skip")
    builder.button(text="Отменить ❌", callback_data="create_cancel")
    builder.adjust(1)
    return builder.as_markup()

# === АДМИНСКИЕ КОМАНДЫ ===
@dp.message(Command("add_admin"))
async def add_admin_cmd(message: types.Message):
    if message.from_user.id != CREATOR_ID:
        return
    try:
        new_admin_id = int(message.text.split()[1])
        admins.add(new_admin_id)
        await message.answer(f"Пользователь {new_admin_id} назначен админом.")
    except:
        await message.answer("Использование: /add_admin <ID_пользователя>")

# Отслеживание добавления бота в группы
@dp.my_chat_member()
async def on_bot_added_to_group(event: types.ChatMemberUpdated):
    if event.new_chat_member.status in ["member", "administrator"]:
        known_groups[event.chat.id] = event.chat.title

# === СОЗДАНИЕ МЕРОПРИЯТИЯ (В ЛИЧКУ БОТУ) ===
@dp.message(CommandStart(), F.chat.type == "private")
async def start_cmd(message: types.Message, state: FSMContext):
    if message.from_user.id not in admins:
        await message.answer("У вас нет прав администратора.")
        return

    admin_groups = []
    # Проверяем, в каких известных группах пользователь - админ
    for group_id, group_name in known_groups.items():
        try:
            member = await bot.get_chat_member(group_id, message.from_user.id)
            if member.status in ['creator', 'administrator']:
                admin_groups.append((group_id, group_name))
        except:
            pass

    if not admin_groups:
        await message.answer("Вы не являетесь администратором ни в одной группе, где есть бот.")
        return

    await state.update_data(admin_groups=admin_groups)
    kb = get_pagination_keyboard(admin_groups, 0, 5, "group")
    await message.answer("Выберите группу для управления мероприятиями:", reply_markup=kb.as_markup())
    await state.set_state(CreateEvent.choosing_group)

# Обработка выбора группы
@dp.callback_query(F.data.startswith("group_select_"), CreateEvent.choosing_group)
async def group_selected(callback: CallbackQuery, state: FSMContext):
    group_id = int(callback.data.split("_")[2])
    group_name = known_groups.get(group_id, "Группа")
    await state.update_data(selected_group=group_id, group_name=group_name)
    
    # Ищем мероприятия этой группы
    group_events = [e for e in events_db.values() if e["group_id"] == group_id]
    
    text = f"Выбрана группа: **{group_name}**\n\n"
    if not group_events:
        text += "Запланированных мероприятий нет.\n"
    else:
        text += f"Всего мероприятий: {len(group_events)}\n"
        
    text += "Напишите /create для создания нового."
    await callback.message.edit_text(text, parse_mode="Markdown")
    await callback.answer()

@dp.message(Command("create"), CreateEvent.choosing_group)
async def start_creation(message: types.Message, state: FSMContext):
    await state.set_state(CreateEvent.waiting_for_name)
    await message.answer("Введите название мероприятия:", reply_markup=get_cancel_skip_kb(allow_skip=False))

# Отмена и Пропуск
@dp.callback_query(F.data == "create_cancel", StateFilter(CreateEvent))
async def cancel_creation(callback: CallbackQuery, state: FSMContext):
    await state.set_state(CreateEvent.choosing_group)
    await callback.message.edit_text("Создание отменено. Выберите группу заново через /start.")
    await callback.answer()

@dp.callback_query(F.data == "create_skip", StateFilter(CreateEvent))
async def skip_step(callback: CallbackQuery, state: FSMContext):
    current_state = await state.get_state()
    # Имитация ввода пустого сообщения
    message = callback.message
    message.text = " " 
    message.from_user = callback.from_user
    await process_creation_step(message, state, is_skip=True)
    await callback.answer()

# Обработка шагов создания
@dp.message(StateFilter(CreateEvent.waiting_for_name, CreateEvent.waiting_for_date, 
                        CreateEvent.waiting_for_time, CreateEvent.waiting_for_location, 
                        CreateEvent.waiting_for_description))
async def process_creation_step(message: types.Message, state: FSMContext, is_skip=False):
    current_state = await state.get_state()
    text = " " if is_skip else message.text

    if current_state == CreateEvent.waiting_for_name.state:
        await state.update_data(name=text)
        await state.set_state(CreateEvent.waiting_for_date)
        await message.answer("Введите дату:", reply_markup=get_cancel_skip_kb())
        
    elif current_state == CreateEvent.waiting_for_date.state:
        await state.update_data(date=text)
        await state.set_state(CreateEvent.waiting_for_time)
        await message.answer("Введите время:", reply_markup=get_cancel_skip_kb())
        
    elif current_state == CreateEvent.waiting_for_time.state:
        await state.update_data(time=text)
        await state.set_state(CreateEvent.waiting_for_location)
        await message.answer("Введите место проведения:", reply_markup=get_cancel_skip_kb())
        
    elif current_state == CreateEvent.waiting_for_location.state:
        await state.update_data(location=text)
        await state.set_state(CreateEvent.waiting_for_description)
        await message.answer("Введите описание:", reply_markup=get_cancel_skip_kb())
        
    elif current_state == CreateEvent.waiting_for_description.state:
        await state.update_data(description=text)
        data = await state.get_data()
        
        host_mention = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
        await state.update_data(host=host_mention)
        
        preview = (f"**{data.get('name', 'Без названия')}**\n"
                   f"**Хост:** {host_mention}\n\n"
                   f"**Дата:**\n{data.get('date', ' ')}\n\n"
                   f"**Время:**\n{data.get('time', ' ')}\n\n"
                   f"**Место проведения:**\n{data.get('location', ' ')}\n\n"
                   f"**Описание:**\n{data.get('description', ' ')}\n\n"
                   f"Нажмите на ✅ чтобы записаться.")
                   
        builder = InlineKeyboardBuilder()
        builder.button(text="Подтвердить", callback_data="confirm_event")
        builder.button(text="Удалить", callback_data="delete_event")
        
        await message.answer("Предпросмотр:\n\n" + preview, reply_markup=builder.as_markup(), parse_mode="Markdown")
        await state.set_state(CreateEvent.confirming)

@dp.callback_query(F.data.in_(["confirm_event", "delete_event"]), CreateEvent.confirming)
async def finalize_event(callback: CallbackQuery, state: FSMContext):
    if callback.data == "confirm_event":
        data = await state.get_data()
        event_id = len(events_db) + 1
        events_db[event_id] = {
            "group_id": data['selected_group'],
            "name": data['name'],
            "date": data['date'],
            "time": data['time'],
            "location": data['location'],
            "description": data['description'],
            "host": data['host']
        }
        await callback.message.edit_text(f"Мероприятие для группы {data['group_name']} создано!")
    else:
        await callback.message.edit_text("Создание отменено.")
        
    await state.set_state(CreateEvent.choosing_group)
    await callback.answer()

# === РАБОТА В ГРУППЕ ===
@dp.message(Command("events"), F.chat.type.in_(["group", "supergroup"]))
async def group_events_cmd(message: types.Message):
    await message.delete()  # Удаляем команду
    
    # Проверка на админа
    member = await bot.get_chat_member(message.chat.id, message.from_user.id)
    if member.status not in ['creator', 'administrator'] and message.from_user.id not in admins:
        return

    group_events = [(eid, e["name"]) for eid, e in events_db.items() if e["group_id"] == message.chat.id]
    
    if not group_events:
        msg = await message.answer("В этой группе нет запланированных мероприятий.")
        await asyncio.sleep(5)
        await msg.delete()
        return

    kb = get_pagination_keyboard(group_events, 0, 5, "post_event")
    await message.answer("Выберите мероприятие для публикации:", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("post_event_select_"))
async def post_event_to_group(callback: CallbackQuery):
    event_id = int(callback.data.split("_")[3])
    event = events_db.get(event_id)
    
    if not event:
        await callback.answer("Мероприятие не найдено")
        return

    text = (f"**{event['name']}**\n"
            f"**Хост:** {event['host']}\n\n"
            f"**Дата:**\n{event['date']}\n\n"
            f"**Время:**\n{event['time']}\n\n"
            f"**Место проведения:**\n{event['location']}\n\n"
            f"**Описание:**\n{event['description']}\n\n"
            f"Нажмите на ✅ чтобы записаться.")
            
    builder = InlineKeyboardBuilder()
    builder.button(text="✅", callback_data=f"attend_{event_id}")
    
    await callback.message.delete() # Удаляем меню выбора
    sent_msg = await bot.send_message(callback.message.chat.id, text, reply_markup=builder.as_markup(), parse_mode="Markdown")
    
    # Сохраняем пост как активный
    active_posts[sent_msg.message_id] = {
        "event_id": event_id,
        "group_id": callback.message.chat.id,
        "attendees": {}  # user_id: mention_string
    }
    await callback.answer()

# Нажатие на галочку
@dp.callback_query(F.data.startswith("attend_"))
async def attend_event(callback: CallbackQuery):
    msg_id = callback.message.message_id
    if msg_id not in active_posts:
        await callback.answer("Этот пост больше не активен.", show_alert=True)
        return
        
    user_id = callback.from_user.id
    if user_id in active_posts[msg_id]["attendees"]:
        await callback.answer("Вы уже записаны!", show_alert=True)
        return
        
    mention = f"@{callback.from_user.username}" if callback.from_user.username else f"[{callback.from_user.first_name}](tg://user?id={user_id})"
    active_posts[msg_id]["attendees"][user_id] = mention
    
    await callback.answer("Вы успешно записались!")

# Завершение набора (/finish)
@dp.message(Command("finish"), F.chat.type.in_(["group", "supergroup"]))
async def finish_cmd(message: types.Message):
    await message.delete()
    
    member = await bot.get_chat_member(message.chat.id, message.from_user.id)
    if member.status not in ['creator', 'administrator'] and message.from_user.id not in admins:
        return

    # Ищем активные посты в этой группе
    group_active_posts = []
    for msg_id, data in active_posts.items():
        if data["group_id"] == message.chat.id:
            event_name = events_db[data["event_id"]]["name"]
            group_active_posts.append((msg_id, event_name))

    if not group_active_posts:
        msg = await message.answer("Нет активных сборов на мероприятия.")
        await asyncio.sleep(5)
        await msg.delete()
        return

    kb = get_pagination_keyboard(group_active_posts, 0, 5, "finish_post")
    await message.answer("Выберите мероприятие для завершения набора:", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("finish_post_select_"))
async def process_finish(callback: CallbackQuery):
    msg_id = int(callback.data.split("_")[3])
    post_data = active_posts.get(msg_id)
    
    if not post_data:
        await callback.answer("Пост не найден.")
        return
        
    event = events_db[post_data["event_id"]]
    attendees = list(post_data["attendees"].values())
    
    await callback.message.delete() # Удаляем меню выбора
    try:
        await bot.delete_message(callback.message.chat.id, msg_id) # Удаляем сам пост с галочкой
    except:
        pass # Если пост уже удален вручную
        
    text = f"Сбор на **{event['name']}** завершен!\n\n**Участники:**\n"
    if attendees:
        text += "\n".join(attendees)
    else:
        text += "Никто не записался 😢"
        
    await bot.send_message(callback.message.chat.id, text, parse_mode="Markdown")
    del active_posts[msg_id] # Удаляем из активных
    await callback.answer()

# Обработка пустых кнопок пагинации (чтобы не висели часики)
@dp.callback_query(F.data == "ignore")
async def ignore_callback(callback: CallbackQuery):
    await callback.answer()

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())