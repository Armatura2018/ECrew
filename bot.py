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
forum_topics = {}  # chat_id: {thread_id: topic_name}
events_db = {}     # event_id: {group_id, name, date, time, location, description, creator_id, host_username}
active_posts = {}  # message_id (в группе): {event_id, group_id, attendees: set()}
action_logs = {}   # chat_id: ["событие 1", "событие 2"]

# === СОСТОЯНИЯ (FSM) ===
class CreateEvent(StatesGroup):
    choosing_group = State()
    waiting_for_name = State()
    waiting_for_date = State()
    waiting_for_time = State()
    waiting_for_location = State()
    waiting_for_description = State()
    confirming = State()

class LogsCreator(StatesGroup):
    choosing_group = State()
    choosing_action = State()

class CustomMessage(StatesGroup):
    waiting_for_text = State()
    choosing_topic = State()

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===
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

def get_cancel_skip_kb(allow_skip: bool = True):
    builder = InlineKeyboardBuilder()
    if allow_skip:
        builder.button(text="Пропустить ⏭", callback_data="create_skip")
    builder.button(text="Отменить ❌", callback_data="create_cancel")
    builder.adjust(1)
    return builder.as_markup()

async def check_user_is_admin(chat_id: int, user_id: int) -> bool:
    if user_id in admins:
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ['creator', 'administrator']
    except:
        return False

def log_action(group_id: int, action_text: str):
    if group_id not in action_logs:
        action_logs[group_id] = []
    action_logs[group_id].append(action_text)

# === ОТСЛЕЖИВАНИЕ ГРУПП И ТОПИКОВ ===
@dp.my_chat_member()
async def on_bot_added_to_group(event: types.ChatMemberUpdated):
    if event.new_chat_member.status in ["member", "administrator"]:
        known_groups[event.chat.id] = event.chat.title

@dp.message(F.forum_topic_created)
async def track_new_topic(message: types.Message):
    chat_id = message.chat.id
    thread_id = message.message_thread_id
    topic_name = message.forum_topic_created.name
    
    if chat_id not in forum_topics:
        forum_topics[chat_id] = {}
    forum_topics[chat_id][thread_id] = topic_name

# === АДМИНСКИЕ КОМАНДЫ (НАЗНАЧЕНИЕ) ===
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

# === ПАНЕЛЬ СОЗДАТЕЛЯ: /logs ===
@dp.message(Command("logs"), F.chat.type == "private")
async def logs_cmd(message: types.Message, state: FSMContext):
    if message.from_user.id != CREATOR_ID:
        return
    if not known_groups:
        return await message.answer("Бот пока не добавлен ни в одну группу.")
        
    groups_list = list(known_groups.items())
    kb = get_pagination_keyboard(groups_list, 0, 5, "logsg")
    await message.answer(" Список всех групп (Логи и управление):", reply_markup=kb.as_markup())
    await state.set_state(LogsCreator.choosing_group)

@dp.callback_query(F.data.startswith("logsg_select_"), LogsCreator.choosing_group)
async def log_group_selected(callback: CallbackQuery, state: FSMContext):
    group_id = int(callback.data.split("_")[2])
    group_name = known_groups.get(group_id, "Группа")
    await state.update_data(target_group_id=group_id)
    
    builder = InlineKeyboardBuilder()
    builder.button(text="Посмотреть логи", callback_data="logs_view")
    builder.button(text="Отправить сообщение", callback_data="logs_send_msg")
    builder.adjust(1)
    
    await callback.message.edit_text(f"Выбрана группа: **{group_name}**\nЧто нужно сделать?", parse_mode="Markdown", reply_markup=builder.as_markup())
    await state.set_state(LogsCreator.choosing_action)

@dp.callback_query(F.data == "logs_view", LogsCreator.choosing_action)
async def view_logs(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    group_id = data['target_group_id']
    logs = action_logs.get(group_id, [])
    
    if not logs:
        text = " Логи пусты. Для этой группы еще ничего не создавалось."
    else:
        text = " **Логи группы:**\n\n" + "\n".join(logs)
        
    await callback.message.edit_text(text, parse_mode="Markdown")
    await state.clear()
    await callback.answer()

@dp.callback_query(F.data == "logs_send_msg", LogsCreator.choosing_action)
async def start_custom_msg_creator(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Напишите сообщение, которое нужно отправить в группу от имени бота:")
    await state.set_state(CustomMessage.waiting_for_text)
    await callback.answer()

# === ПАНЕЛЬ УПРАВЛЕНИЯ (/start В ЛС ДЛЯ АДМИНОВ И СОЗДАТЕЛЯ) ===
@dp.message(CommandStart(), F.chat.type == "private")
async def start_cmd(message: types.Message, state: FSMContext):
    if message.from_user.id not in admins:
        await message.answer("У вас нет прав администратора.")
        return

    admin_groups = []
    for group_id, group_name in known_groups.items():
        if await check_user_is_admin(group_id, message.from_user.id):
            admin_groups.append((group_id, group_name))

    if not admin_groups:
        await message.answer("Вы не являетесь администратором ни в одной группе, где есть бот.")
        return

    await state.update_data(admin_groups=admin_groups)
    kb = get_pagination_keyboard(admin_groups, 0, 5, "group")
    await message.answer("Выберите группу, где вы администратор:", reply_markup=kb.as_markup())
    await state.set_state(CreateEvent.choosing_group)

@dp.callback_query(F.data.startswith("group_select_"), CreateEvent.choosing_group)
async def group_selected(callback: CallbackQuery, state: FSMContext):
    group_id = int(callback.data.split("_")[2])
    group_name = known_groups.get(group_id, "Группа")
    await state.update_data(selected_group=group_id, group_name=group_name)
    
    group_events = [e for e in events_db.values() if e["group_id"] == group_id]
    text = f"Выбрана группа: **{group_name}**\n"
    text += f"Всего запланированных мероприятий: {len(group_events)}\n\nВыберите действие:"
    
    builder = InlineKeyboardBuilder()
    builder.button(text=" Создать мероприятие", callback_data="admin_create_event")
    builder.button(text=" Отправить сообщение", callback_data="admin_send_msg")
    builder.adjust(1)

    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    await callback.answer()

# === ОТПРАВКА ПРОИЗВОЛЬНЫХ СООБЩЕНИЙ В ГРУППУ (АДМИНЫ И СОЗДАТЕЛЬ) ===
@dp.callback_query(F.data == "admin_send_msg", CreateEvent.choosing_group)
async def start_custom_msg_admin(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.update_data(target_group_id=data['selected_group'])
    await callback.message.edit_text("Напишите сообщение, которое нужно отправить от имени бота:")
    await state.set_state(CustomMessage.waiting_for_text)
    await callback.answer()

@dp.message(CustomMessage.waiting_for_text)
async def custom_msg_text_received(message: types.Message, state: FSMContext):
    await state.update_data(msg_text=message.text)
    data = await state.get_data()
    group_id = data['target_group_id']
    
    try:
        chat = await bot.get_chat(group_id)
        if chat.is_forum:
            topics = forum_topics.get(group_id, {})
            builder = InlineKeyboardBuilder()
            builder.button(text="В текущую / Общую", callback_data="send_custom_0")
            for th_id, th_name in topics.items():
                builder.button(text=th_name, callback_data=f"send_custom_{th_id}")
            builder.adjust(1)
            
            await message.answer("Группа является форумом. Выберите категорию (топик):", reply_markup=builder.as_markup())
            await state.set_state(CustomMessage.choosing_topic)
            return
    except:
        pass # Если не удалось проверить статус форума, шлем как обычно
        
    # Отправка в обычную группу
    try:
        await bot.send_message(group_id, message.text)
        await message.answer("✅ Сообщение успешно отправлено в группу!")
    except Exception as e:
        await message.answer(f"❌ Ошибка отправки: {e}")
    await state.clear()

@dp.callback_query(F.data.startswith("send_custom_"), CustomMessage.choosing_topic)
async def custom_msg_topic_selected(callback: CallbackQuery, state: FSMContext):
    thread_id = int(callback.data.split("_")[2])
    data = await state.get_data()
    group_id = data['target_group_id']
    text = data['msg_text']
    
    try:
        await bot.send_message(
            group_id, 
            text, 
            message_thread_id=thread_id if thread_id != 0 else None
        )
        await callback.message.edit_text("✅ Сообщение успешно отправлено в выбранную категорию!")
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка отправки: {e}")
    
    await state.clear()
    await callback.answer()

# === СОЗДАНИЕ МЕРОПРИЯТИЯ ===
@dp.callback_query(F.data == "admin_create_event", CreateEvent.choosing_group)
@dp.message(Command("create"), CreateEvent.choosing_group)
async def start_creation(update: types.Message | CallbackQuery, state: FSMContext):
    await state.set_state(CreateEvent.waiting_for_name)
    msg = update.message if isinstance(update, CallbackQuery) else update
    
    if isinstance(update, CallbackQuery):
        await update.message.edit_text("Введите название мероприятия:", reply_markup=get_cancel_skip_kb(allow_skip=False))
        await update.answer()
    else:
        await msg.answer("Введите название мероприятия:", reply_markup=get_cancel_skip_kb(allow_skip=False))

@dp.callback_query(F.data == "create_cancel", StateFilter(CreateEvent))
async def cancel_creation(callback: CallbackQuery, state: FSMContext):
    await state.set_state(CreateEvent.choosing_group)
    await callback.message.edit_text("Создание отменено. Выберите группу заново через /start.")
    await callback.answer()

@dp.callback_query(F.data == "create_skip", StateFilter(CreateEvent))
async def skip_step(callback: CallbackQuery, state: FSMContext):
    message = callback.message
    message.text = " " 
    message.from_user = callback.from_user
    await process_creation_step(message, state, is_skip=True)
    await callback.answer()

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
        group_id = data['selected_group']
        event_id = len(events_db) + 1
        
        events_db[event_id] = {
            "group_id": group_id,
            "name": data['name'],
            "date": data['date'],
            "time": data['time'],
            "location": data['location'],
            "description": data['description'],
            "host": data['host']
        }
        
        # ЗАПИСЬ В ЛОГИ
        log_action(group_id, f"✅ **Создано мероприятие:** {data['name']}")
        await callback.message.edit_text(f"Мероприятие для группы {data['group_name']} успешно создано!")
    else:
        await callback.message.edit_text("Создание отменено.")
        
    await state.set_state(CreateEvent.choosing_group)
    await callback.answer()

# === РАБОТА В ГРУППЕ (/events, /finish) ===
@dp.message(Command("events"), F.chat.type.in_(["group", "supergroup"]))
async def group_events_cmd(message: types.Message):
    await message.delete()
    if not await check_user_is_admin(message.chat.id, message.from_user.id):
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
async def choose_topic_for_event(callback: CallbackQuery):
    if not await check_user_is_admin(callback.message.chat.id, callback.from_user.id):
        return await callback.answer("Только администраторы могут использовать это меню!", show_alert=True)

    event_id = int(callback.data.split("_")[3])
    
    if callback.message.chat.is_forum:
        topics = forum_topics.get(callback.message.chat.id, {})
        builder = InlineKeyboardBuilder()
        builder.button(text="В текущую / Общую", callback_data=f"send_ev_{event_id}_0")
        for th_id, th_name in topics.items():
            builder.button(text=th_name, callback_data=f"send_ev_{event_id}_{th_id}")
        builder.adjust(1)
        await callback.message.edit_text("Выберите категорию (топик) для отправки:", reply_markup=builder.as_markup())
    else:
        await send_event_announcement(callback, event_id, None)

@dp.callback_query(F.data.startswith("send_ev_"))
async def process_send_event(callback: CallbackQuery):
    if not await check_user_is_admin(callback.message.chat.id, callback.from_user.id):
        return await callback.answer("Отказано в доступе.", show_alert=True)

    parts = callback.data.split("_")
    event_id = int(parts[2])
    thread_id = int(parts[3])
    await send_event_announcement(callback, event_id, thread_id if thread_id != 0 else None)

async def send_event_announcement(callback: CallbackQuery, event_id: int, thread_id: Optional[int]):
    event = events_db.get(event_id)
    if not event:
        return await callback.answer("Мероприятие не найдено")

    text = (f"**{event['name']}**\n"
            f"**Хост:** {event['host']}\n\n"
            f"**Дата:**\n{event['date']}\n\n"
            f"**Время:**\n{event['time']}\n\n"
            f"**Место проведения:**\n{event['location']}\n\n"
            f"**Описание:**\n{event['description']}\n\n"
            f"Нажмите на ✅ чтобы записаться.")
            
    builder = InlineKeyboardBuilder()
    builder.button(text="✅", callback_data=f"attend_{event_id}")
    
    await callback.message.delete()
    sent_msg = await bot.send_message(
        callback.message.chat.id, text, reply_markup=builder.as_markup(), 
        parse_mode="Markdown", message_thread_id=thread_id
    )
    
    active_posts[sent_msg.message_id] = {
        "event_id": event_id, "group_id": callback.message.chat.id, "attendees": {}
    }
    await callback.answer()

@dp.callback_query(F.data.startswith("attend_"))
async def attend_event(callback: CallbackQuery):
    msg_id = callback.message.message_id
    if msg_id not in active_posts:
        return await callback.answer("Этот пост больше не активен.", show_alert=True)
        
    user_id = callback.from_user.id
    if user_id in active_posts[msg_id]["attendees"]:
        return await callback.answer("Вы уже записаны!", show_alert=True)
        
    mention = f"@{callback.from_user.username}" if callback.from_user.username else f"[{callback.from_user.first_name}](tg://user?id={user_id})"
    active_posts[msg_id]["attendees"][user_id] = mention
    await callback.answer("Вы успешно записались!")

@dp.message(Command("finish"), F.chat.type.in_(["group", "supergroup"]))
async def finish_cmd(message: types.Message):
    await message.delete()
    if not await check_user_is_admin(message.chat.id, message.from_user.id):
        return

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
async def choose_topic_for_finish(callback: CallbackQuery):
    if not await check_user_is_admin(callback.message.chat.id, callback.from_user.id):
        return await callback.answer("Только для админов!", show_alert=True)

    msg_id = int(callback.data.split("_")[3])
    
    if callback.message.chat.is_forum:
        topics = forum_topics.get(callback.message.chat.id, {})
        builder = InlineKeyboardBuilder()
        builder.button(text="В текущую / Общую", callback_data=f"send_fin_{msg_id}_0")
        for th_id, th_name in topics.items():
            builder.button(text=th_name, callback_data=f"send_fin_{msg_id}_{th_id}")
        builder.adjust(1)
        await callback.message.edit_text("Выберите категорию для отправки итогов:", reply_markup=builder.as_markup())
    else:
        await send_finish_message(callback, msg_id, None)

@dp.callback_query(F.data.startswith("send_fin_"))
async def process_send_finish(callback: CallbackQuery):
    if not await check_user_is_admin(callback.message.chat.id, callback.from_user.id):
        return await callback.answer("Отказано в доступе.", show_alert=True)

    parts = callback.data.split("_")
    msg_id = int(parts[2])
    thread_id = int(parts[3])
    await send_finish_message(callback, msg_id, thread_id if thread_id != 0 else None)

async def send_finish_message(callback: CallbackQuery, msg_id: int, thread_id: Optional[int]):
    post_data = active_posts.get(msg_id)
    if not post_data:
        return await callback.answer("Пост не найден.")
        
    event = events_db[post_data["event_id"]]
    group_id = post_data["group_id"]
    attendees = list(post_data["attendees"].values())
    
    await callback.message.delete() 
    try:
        await bot.delete_message(group_id, msg_id)
    except:
        pass 
        
    text = f"Сбор на **{event['name']}** завершен!\n\n**Участники:**\n"
    if attendees:
        text += "\n".join(attendees)
    else:
        text += "Никто не записался"
        
    await bot.send_message(group_id, text, parse_mode="Markdown", message_thread_id=thread_id)
    
    # ЗАПИСЬ В ЛОГИ
    log_action(group_id, f"🏁 **Завершен сбор на мероприятие:** {event['name']} (Участников: {len(attendees)})")
    del active_posts[msg_id]
    await callback.answer()

@dp.callback_query(F.data == "ignore")
async def ignore_callback(callback: CallbackQuery):
    await callback.answer()

@dp.callback_query(F.data.regexp(r"^(post_event_page_|finish_post_page_)"))
async def protect_pagination(callback: CallbackQuery):
    if callback.message.chat.type in ["group", "supergroup"]:
        if not await check_user_is_admin(callback.message.chat.id, callback.from_user.id):
            return await callback.answer("Листать меню могут только администраторы!", show_alert=True)
    await callback.answer("Эта страница в разработке (перелистывание).")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())