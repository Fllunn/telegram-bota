import re
import schedule
import time
import threading
import hashlib
import os
import json
import random
import telebot
from dotenv import load_dotenv
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove, ReplyKeyboardMarkup, \
    KeyboardButton
import re

# Загружаем переменные окружения из .env
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is not set!")

bot = telebot.TeleBot(BOT_TOKEN)

EDIT_WORD_PREFIX = "edit_word:"
SEARCH_CHANGE_PREFIX = "search_word_change:"


def generate_id(question):
    """Генерация короткого идентификатора для вопроса"""
    return hashlib.md5(question.encode('utf-8')).hexdigest()[:8]


def contains_invalid_symbols(text):
    """Проверка текста на наличие запрещенных символов."""
    global allowed_symbols
    # Загружаем список разрешенных символов из файла
    allowed_symbols = set(load_json("allowed_symbols.json", {}).get("allowed", ""))
    if not allowed_symbols:
        raise ValueError("Список разрешенных символов пуст или не найден в allowed_symbols.json")

    # Убираем переносы строк и пробелы для проверки
    cleaned_text = text.replace("\n", "").replace(" ", "")
    # Проверяем каждый символ текста на запрещенные символы
    return any(char not in allowed_symbols for char in cleaned_text)


def has_excessive_repetition(text, max_repeats=10):
    text = text.replace("\n", "").replace(" ", "")
    """Проверяет, есть ли в тексте слишком много повторяющихся символов подряд."""
    from itertools import groupby
    for char, group in groupby(text):
        if len(list(group)) > max_repeats:
            return True
    return False


def generate_question_hash(data):
    """Создаёт короткий хэш для данных (не более 64 байт)"""
    return hashlib.sha256(data.encode('utf-8')).hexdigest()[:16]  # Хэш ограничен до 16 символов


# Функции для работы с файлами
def load_json(filename, default=None):
    if os.path.exists(filename):
        try:
            with open(filename, "r", encoding="utf-8") as file:
                return json.load(file)
        except json.JSONDecodeError:
            return default or {}
    return default or {}


quiz_modes = load_json("quiz_modes.json", {})  # Режимы викторины пользователей


def generate_category_hash(category_name):
    """Создает хэш для названия категории"""
    return hashlib.md5(category_name.encode('utf-8')).hexdigest()[:16]


def save_json(filename, data):
    with open(filename, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=4)


# Инициализация данных
user_categories = load_json("user_categories.json", {})
errors = load_json("errors.json", {})
user_context = {}
add_word_context = {}
categories_for_all_users = load_json("categories_for_all_users.json", {})
allowed_users = load_json("allowed_users.json", [])
# Загрузка разрешенных символов
allowed_symbols = set(load_json("allowed_symbols.json", {}).get("allowed", ""))


def natural_sort_key(s):
    return [
        int(text) if text.isdigit() else text.lower()
        for text in re.split(r'(\d+)', s)
    ]


@bot.message_handler(commands=['start'])
def start_message(message):
    user_id = str(message.chat.id)

    if user_id not in user_categories:
        user_categories[user_id] = {}
        save_json("user_categories.json", user_categories)

    categories = sorted(user_categories[user_id].keys(), key=natural_sort_key)  # Естественная сортировка

    if not categories:
        bot.send_message(user_id, "У вас пока нет категорий. Используйте /add_word для добавления слов.")
        return

    # Создаем кнопки с категориями
    markup = InlineKeyboardMarkup()
    for category in categories:
        markup.add(InlineKeyboardButton(category, callback_data=f"category:{category}"))

    bot.send_message(user_id, "Выберите категорию:", reply_markup=markup)


def show_categories(user_id):
    categories = user_categories.get(user_id, {})
    if not categories:
        bot.send_message(user_id, "У вас пока нет категорий. Используйте /add_word для добавления слов.")
        return
    markup = InlineKeyboardMarkup()
    for category in categories.keys():
        markup.add(InlineKeyboardButton(category, callback_data=f"category:{category}"))
    bot.send_message(user_id, "Выбери категорию:", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("category:"))
def select_category(call):
    bot.answer_callback_query(call.id)
    user_id = str(call.message.chat.id)
    selected_category = call.data.split(":")[1]
    # Копируем все вопросы выбранной категории и сбрасываем счётчик правильных ответов
    questions = user_categories[user_id][selected_category][:]
    for q in questions:
        q["correct_count"] = 0
    user_context[user_id] = {
        "mode": "quiz",
        "category": selected_category,
        "all_questions": questions,  # все вопросы категории
        "current_round_questions": questions[:] if questions else [],  # первый круг = все вопросы
        "round_number": 1,  # начинаем с 1-го круга
        "session_errors": {},  # фиксируем ошибки (qid: correct_answer)
        "start_time": time.time()
    }
    bot.send_message(user_id, f"Ты выбрал категорию: {selected_category}.\nНачинается 1 круг викторины.")
    send_quiz(user_id)


def send_quiz(user_id):
    context = user_context.get(user_id)
    if not context or context.get("mode") != "quiz":
        return

    # Проверяем, есть ли вообще вопросы в категории
    if not context.get("all_questions"):
        bot.send_message(user_id, "⚠ Ошибка: в этой категории нет доступных вопросов.")
        user_context.pop(user_id, None)
        return

    # Если все вопросы освоены (по 2 правильных ответа на каждый) – завершаем викторину
    all_mastered = all(q.get("correct_count", 0) == 2 for q in context["all_questions"])
    if all_mastered:
        elapsed = time.time() - context["start_time"]
        elapsed_str = time.strftime("%M:%S", time.gmtime(elapsed))
        error_answers = [q["correct"] for q in context["all_questions"] if
                         generate_id(q["question"]) in context["session_errors"]]

        if error_answers:
            if len(error_answers) > 10:
                context["final_error_list"] = error_answers
                context["final_error_page"] = 0
                bot.send_message(user_id, f"🎉 Викторина завершена!\nВремя: {elapsed_str}")
                send_final_error_page(user_id, 0)
            else:
                numbered = "\n".join([f"{i + 1}. {word}" for i, word in enumerate(error_answers)])
                bot.send_message(user_id, f"🎉 Викторина завершена!\nВремя: {elapsed_str}\nОшибки:\n{numbered}")
        else:
            bot.send_message(user_id, f"🎉 Викторина завершена!\nВремя: {elapsed_str}\nОшибок не было.")
        user_context.pop(user_id, None)
        return

    # Если текущий круг завершён, начинаем новый
    if not context["current_round_questions"]:
        context["round_number"] += 1

        if context["round_number"] <= 2:
            new_round = context["all_questions"][:]  # Все вопросы из категории
        else:
            new_round = [q for q in context["all_questions"] if q.get("correct_count", 0) < 2]  # Неосвоенные вопросы

        if new_round:
            random.shuffle(new_round)  # 🔥 Перемешиваем новый круг
            context["current_round_questions"] = new_round
            bot.send_message(user_id, f"🔄 Начинается {context['round_number']} круг викторины.")
        else:
            bot.send_message(user_id, "⚠ Ошибка: не осталось вопросов для следующего круга.")
            user_context.pop(user_id, None)
            return

    # Перемешиваем вопросы перед выдачей (вдобавок к shuffle при начале круга)
    random.shuffle(context["current_round_questions"])

    # Проверяем, остались ли вопросы после перемешивания
    if context["current_round_questions"]:
        question = context["current_round_questions"].pop(0)
        context["current"] = question

        try:
            wrong_answer, correct_answer = question["question"].split("←")
            options = [wrong_answer.strip(), correct_answer.strip()]
            random.shuffle(options)  # 🔥 Перемешиваем порядок кнопок

            markup = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
            for opt in options:
                markup.add(KeyboardButton(opt))

            bot.send_message(user_id, "Выберите ответ:", reply_markup=markup)
        except Exception as e:
            bot.send_message(user_id, f"⚠ Ошибка при загрузке вопроса: {str(e)}")
            send_quiz(user_id)  # Пробуем отправить следующий вопрос, если этот вызвал ошибку
    else:
        bot.send_message(user_id, "⚠ Ошибка: не осталось вопросов.")
        user_context.pop(user_id, None)


def send_final_error_page(user_id, page=0):
    context = user_context.get(user_id)
    if not context or "final_error_list" not in context:
        return
    error_list = context["final_error_list"]
    total_pages = (len(error_list) - 1) // 10 + 1
    start_idx = page * 10
    end_idx = start_idx + 10
    page_errors = error_list[start_idx:end_idx]
    numbered = "\n".join([f"{i + start_idx + 1}. {word}" for i, word in enumerate(page_errors)])
    message = f"Слова с ошибками (верные ответы):\n{numbered}\nСтраница {page + 1} из {total_pages}"

    markup = InlineKeyboardMarkup()
    if page > 0:
        markup.add(InlineKeyboardButton("⬅ Назад", callback_data=f"final_error_prev:{page - 1}"))
    if end_idx < len(error_list):
        markup.add(InlineKeyboardButton("Вперед ➡", callback_data=f"final_error_next:{page + 1}"))
    bot.send_message(user_id, message, reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("final_error_"))
def paginate_final_errors(call):
    user_id = str(call.message.chat.id)
    _, page_str = call.data.split(":", 1)
    page = int(page_str)
    try:
        bot.delete_message(user_id, call.message.message_id)
    except Exception as e:
        print(f"Ошибка при удалении сообщения: {e}")
    send_final_error_page(user_id, page)
    bot.answer_callback_query(call.id)


@bot.message_handler(func=lambda message: str(message.chat.id) in user_context and
                                          user_context[str(message.chat.id)].get("mode") == "quiz" and
                                          user_context[str(message.chat.id)].get("current"))
def handle_answer(message):
    user_id = str(message.chat.id)
    context = user_context.get(user_id)
    user_answer = message.text.strip()

    # Если введена команда – завершаем викторину
    if user_answer.startswith("/"):
        user_context.pop(user_id, None)
        bot.send_message(user_id, "Команда принята!", reply_markup=ReplyKeyboardRemove())
        bot.process_new_messages([message])
        return

    question = context["current"]
    correct_answer = question["correct"].strip()
    qid = generate_id(question["question"])
    # Получаем выбранную категорию из контекста викторины
    category = context.get("category", "Без категории")

    if user_answer == correct_answer:
        bot.send_message(user_id, "✅ Верно!")
        question["correct_count"] += 1
    else:
        bot.send_message(user_id, f"❌ Неверно! Правильный ответ: {correct_answer}.", reply_markup=ReplyKeyboardRemove())
        question["correct_count"] = 0  # Сброс счетчика

        # Добавляем ошибку в session_errors
        context["session_errors"][qid] = correct_answer

        # Сохраняем ошибку в errors.json с учётом категории
        if user_id not in errors:
            errors[user_id] = {}
        if category not in errors[user_id]:
            errors[user_id][category] = {}
        if question["question"] in errors[user_id][category]:
            errors[user_id][category][question["question"]] += 1
        else:
            errors[user_id][category][question["question"]] = 1
        save_json("errors.json", errors)

    context["current"] = None
    send_quiz(user_id)


@bot.message_handler(commands=['mistakes'])
def show_errors(message):
    user_id = str(message.chat.id)
    if user_id not in errors or not errors[user_id]:
        bot.send_message(user_id, "У вас нет ошибок!")
        return

    # Функция для извлечения числа из названия категории (если оно есть)
    def sort_key(category):
        match = re.search(r'№\s*(\d+)', category)
        return int(match.group(1)) if match else float('inf')

    sorted_categories = sorted(errors[user_id].keys(), key=sort_key)

    markup = InlineKeyboardMarkup()
    for category in sorted_categories:
        markup.add(InlineKeyboardButton(category, callback_data=f"mistakes_category:{category}"))
    bot.send_message(user_id, "Выберите категорию ошибок:", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("mistakes_category:"))
def mistakes_category_handler(call):
    bot.answer_callback_query(call.id)
    user_id = str(call.message.chat.id)
    selected_category = call.data.split(":", 1)[1]
    if user_id not in errors or selected_category not in errors[user_id]:
        bot.send_message(user_id, "Ошибки в этой категории не найдены.")
        return
    # Сортируем ошибки выбранной категории по убыванию количества
    category_errors = sorted(errors[user_id][selected_category].items(), key=lambda x: (-x[1], x[0]))
    user_context[user_id] = {
        "mistakes_action": "view_category_mistakes",
        "mistakes_category": selected_category,
        "mistakes_list": category_errors,
        "page": 0,
        "total_pages": (len(category_errors) - 1) // 10 + 1
    }
    send_category_mistakes_page(user_id)


def send_category_mistakes_page(user_id, page=0):
    context = user_context.get(user_id)
    if not context or "mistakes_list" not in context:
        bot.send_message(user_id, "Сессия просмотра ошибок устарела.")
        return
    mistakes_list = context["mistakes_list"]
    total_errors = len(mistakes_list)
    total_pages = (total_errors - 1) // 10 + 1
    start_idx = page * 10
    end_idx = start_idx + 10
    current_errors = mistakes_list[start_idx:end_idx]

    message_text = f"Ошибки категории '{context['mistakes_category']}' (Всего: {total_errors})\nСтраница {page + 1} из {total_pages}\n\n"
    for i, (q, count) in enumerate(current_errors, start=start_idx + 1):
        correct_part = q.split("←")[1] if "←" in q else q
        message_text += f"{i}. {correct_part} [{count}]\n"

    markup = InlineKeyboardMarkup()
    if page > 0:
        markup.add(InlineKeyboardButton("⬅ Назад", callback_data=f"mistakes_cat_prev:{page - 1}"))
    if end_idx < total_errors:
        markup.add(InlineKeyboardButton("Вперед ➡", callback_data=f"mistakes_cat_next:{page + 1}"))
    markup.add(InlineKeyboardButton("❌ Закрыть", callback_data="mistakes_cat_close"))

    # Сохраняем текущую страницу в контексте
    context["page"] = page

    # Если сообщение уже отправлено, пытаемся его обновить
    if "mistakes_message_id" in context:
        try:
            bot.edit_message_text(
                chat_id=user_id,
                message_id=context["mistakes_message_id"],
                text=message_text,
                reply_markup=markup
            )
        except Exception as e:
            # Если обновление не удалось, отправляем новое сообщение и сохраняем его ID
            msg = bot.send_message(user_id, message_text, reply_markup=markup)
            context["mistakes_message_id"] = msg.message_id
    else:
        msg = bot.send_message(user_id, message_text, reply_markup=markup)
        context["mistakes_message_id"] = msg.message_id


@bot.callback_query_handler(func=lambda call: call.data == "mistakes_cat_close")
def close_category_mistakes(call):
    user_id = str(call.message.chat.id)
    try:
        bot.delete_message(user_id, call.message.message_id)
    except Exception as e:
        print(f"Ошибка при удалении сообщения: {e}")
    user_context.pop(user_id, None)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("mistakes_cat_prev:") or call.data.startswith("mistakes_cat_next:"))
def paginate_category_mistakes(call):
    user_id = str(call.message.chat.id)
    parts = call.data.split(":")
    if len(parts) != 2 or not parts[1].isdigit():
        bot.answer_callback_query(call.id, "Ошибка пагинации")
        return
    new_page = int(parts[1])
    user_context[user_id]["page"] = new_page
    send_category_mistakes_page(user_id, new_page)
    bot.answer_callback_query(call.id)


def send_mistakes_page(user_id, page=0):
    context = user_context.get(user_id)
    if not context or "mistakes" not in context:
        bot.send_message(user_id, "❌ Сессия просмотра ошибок устарела.")
        return

    start_idx = page * 10
    end_idx = start_idx + 10
    current_errors = context["mistakes"][start_idx:end_idx]

    total_errors = len(context["mistakes"])
    total_pages = (total_errors - 1) // 10 + 1

    message = f"📋 Мои ошибки (Всего: {total_errors})\n"
    message += f"Страница {page + 1} из {total_pages}\n\n"
    message += "\n".join(
        f"{i + 1}. {err[0].split('←')[1]} [{err[1]}]"
        for i, err in enumerate(current_errors, start=start_idx)
    )

    markup = InlineKeyboardMarkup()

    if page > 0:
        markup.add(InlineKeyboardButton("⬅ Назад", callback_data=f"mistakes_prev_{page - 1}"))

    if end_idx < len(context["mistakes"]):
        markup.add(InlineKeyboardButton("Вперед ➡", callback_data=f"mistakes_next_{page + 1}"))

    markup.add(InlineKeyboardButton("❌ Закрыть", callback_data="mistakes_close"))

    try:
        if "message_id" in context:
            bot.edit_message_text(
                chat_id=user_id,
                message_id=context["message_id"],
                text=message,
                reply_markup=markup
            )
        else:
            msg = bot.send_message(user_id, message, reply_markup=markup)
            user_context[user_id]["message_id"] = msg.message_id
    except Exception as e:
        print(f"Ошибка при обновлении сообщений: {e}")


@bot.callback_query_handler(func=lambda call: call.data.startswith(("mistakes_prev_", "mistakes_next_")))
def handle_mistakes_pagination(call):
    user_id = str(call.message.chat.id)

    if user_id not in user_context or "mistakes" not in user_context[user_id]:
        bot.answer_callback_query(call.id, "❌ Сессия устарела")
        return

    parts = call.data.split("_")

    if len(parts) != 3 or not parts[2].isdigit():
        bot.answer_callback_query(call.id, "Ошибка пагинации")
        return

    action, page = parts[1], int(parts[2])

    if action == "prev":
        page = max(0, page)
    elif action == "next":
        page = min(len(user_context[user_id]["mistakes"]) // 10, page)

    user_context[user_id]["page"] = page
    send_mistakes_page(user_id, page)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "mistakes_close")
def handle_mistakes_close(call):
    user_id = str(call.message.chat.id)
    user_context.pop(user_id, None)
    bot.delete_message(call.message.chat.id, call.message.message_id)
    bot.answer_callback_query(call.id)


@bot.message_handler(commands=['add_word'])
def add_word(message):
    user_id = str(message.chat.id)
    categories = sorted(user_categories.get(user_id, {}).keys(), key=natural_sort_key)

    if not categories:
        bot.send_message(user_id, "У вас пока нет категорий. Используйте /add_word для создания.")
        return

    markup = InlineKeyboardMarkup()
    for category in categories:
        category_hash = generate_category_hash(category)
        markup.add(InlineKeyboardButton(category, callback_data=f"add_word_category:{category_hash}"))

    markup.add(InlineKeyboardButton("Создать новую категорию", callback_data="add_word_category:new"))
    msg = bot.send_message(user_id, "Выберите категорию или создайте новую:", reply_markup=markup)

    user_context[user_id] = {
        "category_hashes": {generate_category_hash(c): c for c in categories},
        "message_id": msg.message_id
    }


@bot.callback_query_handler(func=lambda call: call.data.startswith("add_word_category:"))
def add_word_category(call):
    bot.answer_callback_query(call.id)
    user_id = str(call.message.chat.id)
    category_hash = call.data.split(":")[1]

    # Если выбрано "Создать новую категорию"
    if category_hash == "new":
        bot.send_message(user_id, "Введите название новой категории:", reply_markup=ReplyKeyboardRemove())
        # Устанавливаем контекст для создания категории
        add_word_context[user_id] = {
            "step": "new_category",
            "category": None
        }
        return

    # Получаем реальное название категории по хэшу (для существующих категорий)
    category_name = user_context.get(user_id, {}).get("category_hashes", {}).get(category_hash)

    if not category_name:
        bot.send_message(user_id, "Ошибка: категория не найдена.")
        return

    markup = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.add(KeyboardButton("Обычное добавление"), KeyboardButton("Массовое добавление"))
    bot.send_message(user_id, f"Выберите режим добавления в '{category_name}':", reply_markup=markup)

    add_word_context[user_id] = {
        "category": category_name,
        "step": "choose_mode"
    }


@bot.message_handler(
    func=lambda message: str(message.chat.id) in add_word_context and add_word_context[str(message.chat.id)].get(
        "step") == "choose_mode")
def handle_choose_add_mode(message):
    user_id = str(message.chat.id)
    mode = message.text.strip()

    if mode == "Обычное добавление":
        add_word_context[user_id]["step"] = "add_word"
        bot.send_message(user_id,
                         f"Добавляем слово в категорию '{add_word_context[user_id]['category']}'. Введите слово в формате:\nНеверный вариант (первая строка)\nВерный вариант (вторая строка):")
    elif mode == "Массовое добавление":
        add_word_context[user_id]["bulk_mode"] = True
        add_word_context[user_id]["step"] = "add_words_bulk"
        bot.send_message(user_id,
                         f"Вы вошли в режим массового добавления в категорию '{add_word_context[user_id]['category']}'.\nВводите слова в формате:\nНеверный вариант (первая строка)\nВерный вариант (вторая строка)\n\nКогда закончите, отправьте 'Готово' или команду /done.")


@bot.message_handler(
    func=lambda message: str(message.chat.id) in add_word_context and add_word_context[str(message.chat.id)].get(
        "bulk_mode"))
def handle_bulk_word_addition(message):
    user_id = str(message.chat.id)
    text = message.text.strip()

    # Выход из режима массового добавления
    if text.lower() in ["готово", "/done"]:
        bot.send_message(user_id,
                         f"✅ Массовое добавление в категорию '{add_word_context[user_id]['category']}' завершено.")
        del add_word_context[user_id]  # Очищаем контекст
        return

    # Блокируем "Отменить создание" в массовом режиме
    if text.lower() == "отменить создание":
        bot.send_message(user_id,
                         "❌ Вы уже в режиме массового добавления. Чтобы выйти, отправьте 'Готово' или команду /done.")
        return

    # Разделяем вводимые слова по двойному переводу строки (каждая пара слов - отдельный блок)
    words = text.split("\n\n")
    category = add_word_context[user_id]["category"]
    added_count = 0  # Счетчик добавленных слов

    for word_entry in words:
        word_data = word_entry.strip().split("\n")  # Разделяем на строки

        # Проверяем, что в вводе ровно 2 строки (неверный и верный вариант)
        if len(word_data) != 2:
            bot.send_message(user_id,
                             "❌ Ошибка! Введите слово в формате:\nНеверный вариант (первая строка)\nВерный вариант (вторая строка).")
            continue

        wrong_word, correct_word = word_data

        new_word = {
            "question": f"{wrong_word.strip()}←{correct_word.strip()}",
            "correct": correct_word.strip()
        }

        user_categories[user_id][category].append(new_word)
        added_count += 1

    if added_count > 0:
        save_json("user_categories.json", user_categories)
        bot.send_message(user_id,
                         f"✅ Добавлено {added_count} слов в категорию '{category}'. Продолжайте вводить или отправьте 'Готово'.")


# Загрузка данных
categories_for_all_users = load_json("categories_for_all_users.json", {})
allowed_users = load_json("allowed_users.json", [])


@bot.message_handler(func=lambda message: str(message.chat.id) in add_word_context)
def handle_add_word_steps(message):
    user_id = str(message.chat.id)
    step = add_word_context[user_id].get("step")

    if message.text.strip() == "Отменить создание":
        bot.send_message(user_id, "Создание категории отменено.", reply_markup=ReplyKeyboardRemove())
        del add_word_context[user_id]
        return

    if step == "new_category":
        category_name = message.text.strip()

        # Проверяем наличие запрещенных символов
        if contains_invalid_symbols(category_name):
            bot.send_message(user_id, "Недопустимо! Название категории содержит запрещенные символы.")
            return

        # Проверяем длину названия категории
        if len(category_name) > 100 or has_excessive_repetition(category_name):
            bot.send_message(user_id, "Ошибка! Название категории не должно превышать 100 символов.")
            return

        # Инициализируем словарь для пользователя, если он ещё не существует
        if user_id not in user_categories:
            user_categories[user_id] = {}

        # Создаем новую категорию
        user_categories[user_id][category_name] = []
        save_json("user_categories.json", user_categories)

        add_word_context[user_id]["category"] = category_name
        bot.send_message(
            user_id,
            f"Категория '{category_name}' создана. Теперь введите слово в формате:\n"
            "Неверный вариант (первая строка)\n"
            "Верный вариант (вторая строка):",
            parse_mode="HTML"
        )
        add_word_context[user_id]["step"] = "add_word"

    elif step == "add_word":
        # Проверяем наличие запрещённых символов
        if contains_invalid_symbols(message.text):
            bot.send_message(user_id, "Недопустимо! Сообщение содержит запрещённые символы.")
            return

        category = add_word_context[user_id]["category"]
        word_data = message.text.strip().split("\n", 1)

        # Проверяем, что ввод состоит из двух строк
        if len(word_data) != 2:
            bot.send_message(
                user_id,
                "Ошибка! Введите слово в формате:\n"
                "Неверный вариант (первая строка)\n"
                "Верный вариант (вторая строка):",
                parse_mode="HTML"
            )
            return

        wrong_word, correct_word = word_data

        # Проверяем длину каждого слова
        if len(wrong_word.strip()) > 50 or len(correct_word.strip()) > 50 or has_excessive_repetition(
                correct_word.strip()):
            bot.send_message(
                user_id,
                "Ошибка! Каждое слово (и неверный, и верный варианты) не должно превышать 50 символов."
            )
            return

        # Добавляем слово в категорию
        new_word = {
            "question": f"{wrong_word.strip()}←{correct_word.strip()}",
            "correct": correct_word.strip()
        }

        user_categories[user_id][category].append(new_word)
        save_json("user_categories.json", user_categories)

        # Если пользователь в списке разрешённых, обновляем общие категории
        if user_id in allowed_users:
            if category not in categories_for_all_users:
                categories_for_all_users[category] = []
            categories_for_all_users[category].append(new_word)
            save_json("categories_for_all_users.json", categories_for_all_users)

        bot.send_message(
            user_id,
            f"Слово добавлено в категорию '{category}':\nНеверный: {wrong_word}\nВерный: {correct_word}"
        )
        del add_word_context[user_id]


# Удаление категорий или слов
@bot.message_handler(commands=['remove_word'])
def remove_word(message):
    """Выбор действия для удаления (категория или слово)."""
    user_id = str(message.chat.id)
    if user_id not in user_categories or not user_categories[user_id]:
        bot.send_message(user_id, "У вас нет категорий или слов для удаления.")
        return

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("Удалить категорию", callback_data="remove_category_menu"))
    markup.add(InlineKeyboardButton("Удалить слово", callback_data="remove_word_menu"))
    bot.send_message(user_id, "Выберите действие для удаления:", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data == "remove_category_menu")
def show_categories_for_removal(call):
    bot.answer_callback_query(call.id)
    user_id = str(call.message.chat.id)
    categories = sorted(user_categories.get(user_id, {}).keys(), key=natural_sort_key)

    if not categories:
        bot.send_message(user_id, "У вас нет категорий для удаления.")
        return

    markup = InlineKeyboardMarkup()
    category_hash_map = {}
    for category in categories:
        category_hash = generate_category_hash(category)
        category_hash_map[category_hash] = category
        markup.add(InlineKeyboardButton(category, callback_data=f"confirm_remove_category:{category_hash}"))

    user_context[user_id] = {
        "category_hash_map": category_hash_map,
        "message_id": call.message.message_id
    }

    bot.edit_message_text(
        chat_id=user_id,
        message_id=call.message.message_id,
        text="Выберите категорию для удаления:",
        reply_markup=markup
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_remove_category:"))
def confirm_remove_category(call):
    """Запрашиваем подтверждение удаления категории."""
    bot.answer_callback_query(call.id)
    user_id = str(call.message.chat.id)
    category_hash = call.data.split(":")[1]

    category_hash_map = user_context.get(user_id, {}).get("category_hash_map", {})
    category_name = category_hash_map.get(category_hash)

    if not category_name:
        bot.send_message(user_id, "Ошибка: категория не найдена.")
        return

    user_context[user_id]["delete_category"] = category_name  # Сохраняем для подтверждения

    markup = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.add(KeyboardButton("1"), KeyboardButton("0"))

    bot.send_message(
        user_id,
        f"Вы уверены, что хотите удалить категорию '{category_name}'?\n"
        "Нажмите 1 для удаления или 0 для отмены.",
        reply_markup=markup
    )


WORDS_PER_PAGE = 10  # Количество слов на одной странице


@bot.callback_query_handler(func=lambda call: call.data == "remove_word_menu")
def show_categories_to_choose_word(call):
    bot.answer_callback_query(call.id)
    user_id = str(call.message.chat.id)

    # Сортировка категорий с естественным порядком
    categories = sorted(
        user_categories.get(user_id, {}).keys(),
        key=natural_sort_key
    )

    if not categories:
        bot.send_message(user_id, "У вас нет категорий для удаления слов.")
        return

    markup = InlineKeyboardMarkup()
    category_hash_map = {}

    for category in categories:
        category_hash = generate_category_hash(category)
        category_hash_map[category_hash] = category
        markup.add(InlineKeyboardButton(
            category,
            callback_data=f"search_word_to_remove:{category_hash}"
        ))

    user_context[user_id] = {
        "category_hash_map": category_hash_map,
        "action": "remove_word"
    }

    bot.edit_message_text(
        chat_id=user_id,
        message_id=call.message.message_id,
        text="Выберите категорию для удаления слова:",
        reply_markup=markup
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("choose_word_to_remove:"))
def show_words_for_removal(call):
    """Показываем слова в выбранной категории с постраничной навигацией."""
    bot.answer_callback_query(call.id)
    user_id = str(call.message.chat.id)
    category_hash = call.data.split(":")[1]

    # Проверяем, есть ли кеш категорий
    if user_id not in user_context or "category_hash_map" not in user_context[user_id]:
        bot.send_message(user_id, "Ошибка: данные устарели, попробуйте снова.")
        return

    category_name = user_context[user_id]["category_hash_map"].get(category_hash)
    if not category_name or category_name not in user_categories.get(user_id, {}):
        bot.send_message(user_id, "Ошибка: категория не найдена.")
        return

    words = user_categories[user_id][category_name]
    if not words:
        bot.send_message(user_id, f"В категории '{category_name}' нет слов для удаления.")
        return

    # Сохраняем кеш слов и текущую страницу
    user_context[user_id]["word_list"] = words
    user_context[user_id]["current_page"] = 0
    user_context[user_id]["category_hash"] = category_hash

    send_word_list(user_id)


def send_word_list(user_id):
    """Отправляет список найденных слов с кнопками навигации."""
    if user_id not in user_context or "word_list" not in user_context[user_id]:
        bot.send_message(user_id, "Ошибка: кеш данных устарел, попробуйте снова.")
        return

    words = user_context[user_id]["word_list"]
    category_hash = user_context[user_id]["category_hash"]
    page = user_context[user_id]["current_page"]

    total_pages = (len(words) - 1) // WORDS_PER_PAGE + 1  # Количество страниц
    start = page * WORDS_PER_PAGE
    end = start + WORDS_PER_PAGE

    markup = InlineKeyboardMarkup()
    word_hash_map = {}

    for word in words[start:end]:  # Показываем только слова на текущей странице
        question_text = word["question"].replace("←", " / ")
        question_hash = generate_id(word["question"])
        word_hash_map[question_hash] = word["question"]
        markup.add(
            InlineKeyboardButton(question_text, callback_data=f"confirm_remove_word:{category_hash}:{question_hash}"))

    # Кнопки "⏪ Назад" и "⏩ Далее"
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⏪ Назад", callback_data="prev_page"))
    if end < len(words):
        nav_buttons.append(InlineKeyboardButton("⏩ Далее", callback_data="next_page"))

    if nav_buttons:
        markup.row(*nav_buttons)  # Добавляем кнопки в одну строку

    user_context[user_id]["word_hash_map"] = word_hash_map  # Сохраняем кеш хэшей слов
    bot.send_message(user_id, f"📖 Страница {page + 1} из {total_pages}\nВыберите слово для удаления:",
                     reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data in ["prev_page", "next_page"])
def paginate_words(call):
    """Переключает страницы списка слов."""
    bot.answer_callback_query(call.id)
    user_id = str(call.message.chat.id)

    if user_id not in user_context or "current_page" not in user_context[user_id]:
        bot.send_message(user_id, "Ошибка: кеш данных устарел, попробуйте снова.")
        return

    if call.data == "prev_page":
        user_context[user_id]["current_page"] -= 1
    elif call.data == "next_page":
        user_context[user_id]["current_page"] += 1

    send_word_list(user_id)  # Отправляем обновленный список слов


@bot.callback_query_handler(func=lambda call: call.data.startswith("search_word_to_remove:"))
def ask_for_search_word(call):
    """Запрашиваем у пользователя слово для поиска в категории перед удалением."""
    bot.answer_callback_query(call.id)
    user_id = str(call.message.chat.id)
    category_hash = call.data.split(":")[1]

    # Проверяем, есть ли кеш категорий
    if user_id not in user_context or "category_hash_map" not in user_context[user_id]:
        bot.send_message(user_id, "Ошибка: данные устарели, попробуйте снова.")
        return

    category_name = user_context[user_id]["category_hash_map"].get(category_hash)
    if not category_name or category_name not in user_categories.get(user_id, {}):
        bot.send_message(user_id, "Ошибка: категория не найдена.")
        return

    # Сохраняем выбранную категорию в контексте
    user_context[user_id]["category_hash"] = category_hash
    user_context[user_id]["search_mode"] = True  # Включаем режим поиска

    bot.send_message(user_id, f"🔎 Введите часть слова, которое хотите удалить из категории '{category_name}':")


@bot.message_handler(
    func=lambda message: str(message.chat.id) in user_context and user_context[str(message.chat.id)].get("search_mode"))
def search_word_to_remove(message):
    """Фильтруем слова в категории по введенному запросу."""
    user_id = str(message.chat.id)
    search_query = message.text.strip().lower()

    # Проверяем, есть ли сохраненный хэш категории
    category_hash = user_context[user_id].get("category_hash")
    if not category_hash or "category_hash_map" not in user_context[user_id]:
        bot.send_message(user_id, "Ошибка: данные устарели, попробуйте снова.")
        return

    category_name = user_context[user_id]["category_hash_map"].get(category_hash)
    if not category_name or category_name not in user_categories.get(user_id, {}):
        bot.send_message(user_id, "Ошибка: категория не найдена.")
        return

    words = user_categories[user_id][category_name]

    # Фильтруем слова по вхождению текста
    filtered_words = [word for word in words if search_query in word["question"].lower()]

    if not filtered_words:
        bot.send_message(user_id,
                         f"❌ В категории '{category_name}' не найдено слов, содержащих '{search_query}'. Попробуйте снова.")
        return

    # Сохраняем отфильтрованный список слов в контексте
    user_context[user_id]["word_list"] = filtered_words
    user_context[user_id]["current_page"] = 0
    user_context[user_id]["search_mode"] = False  # Отключаем режим поиска

    send_word_list(user_id)  # Отправляем список найденных слов


@bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_remove_word:"))
def confirm_remove_word(call):
    """Подтверждение удаления слова."""
    bot.answer_callback_query(call.id)
    user_id = str(call.message.chat.id)
    _, category_hash, question_hash = call.data.split(":")

    # Проверяем, есть ли сохраненный контекст
    if user_id not in user_context or "category_hash_map" not in user_context[user_id]:
        bot.send_message(user_id, "⚠ Данные устарели.")
        return show_categories_to_choose_word(call)  # Возвращаем пользователя к выбору категории

    category_name = user_context[user_id]["category_hash_map"].get(category_hash)
    if not category_name:
        bot.send_message(user_id, "Ошибка: категория не найдена. Выберите заново.")
        return show_categories_to_choose_word(call)

    word_hash_map = user_context[user_id].get("word_hash_map", {})
    question_text = word_hash_map.get(question_hash)

    if not question_text:
        bot.send_message(user_id, "Ошибка: слово не найдено. Попробуйте снова.")
        return show_categories_to_choose_word(call)

    # Получаем список слов из категории
    words = user_categories.get(user_id, {}).get(category_name, [])

    # Ищем нужное слово
    word_to_delete = next((word for word in words if word["question"] == question_text), None)

    if not word_to_delete:
        bot.send_message(user_id, "Слово не найдено или уже удалено.")
        return show_categories_to_choose_word(call)

    # Сохраняем данные для подтверждения
    user_context[user_id]["delete_word"] = {
        "category": category_name,
        "word": word_to_delete
    }

    markup = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.add(KeyboardButton("1"), KeyboardButton("0"))

    bot.send_message(
        user_id,
        f"Вы уверены, что хотите удалить слово '{question_text.replace('←', '/')}'? Нажмите 1 для удаления или 0 для отмены.",
        reply_markup=markup
    )


@bot.message_handler(func=lambda message: (
        str(message.chat.id) in user_context and
        "delete_word" in user_context[str(message.chat.id)]
))
def handle_word_deletion_confirmation(message):
    user_id = str(message.chat.id)
    confirmation = message.text.strip()

    # Получаем данные о слове для удаления
    delete_info = user_context.get(user_id, {}).get("delete_word")
    if not delete_info:
        bot.send_message(user_id, "Ошибка: информация для удаления отсутствует.", reply_markup=ReplyKeyboardRemove())
        user_context.pop(user_id, None)
        return

    category = delete_info["category"]
    word_to_delete = delete_info["word"]

    if confirmation == "1":  # Подтверждение удаления
        if category in user_categories.get(user_id, {}):
            # Удаляем слово из категории
            user_categories[user_id][category] = [
                word for word in user_categories[user_id][category]
                if word != word_to_delete
            ]
            # Если категория стала пустой, удаляем её
            if not user_categories[user_id][category]:
                del user_categories[user_id][category]
            save_json("user_categories.json", user_categories)

            # Удаляем связанные ошибки из файла errors.json
            if user_id in errors and category in errors[user_id]:
                errors[user_id][category].pop(word_to_delete["question"], None)
                # Если в категории ошибок больше не осталось, удаляем категорию
                if not errors[user_id][category]:
                    del errors[user_id][category]
                save_json("errors.json", errors)

            bot.send_message(
                user_id,
                f"✅ Слово '{word_to_delete['question'].replace('←', '/')}' удалено из категории '{category}'.",
                reply_markup=ReplyKeyboardRemove()
            )
        else:
            bot.send_message(user_id, f"Ошибка: категория '{category}' не найдена.", reply_markup=ReplyKeyboardRemove())
        user_context.pop(user_id, None)
    elif confirmation == "0":  # Отмена удаления
        bot.send_message(user_id, "Удаление отменено.", reply_markup=ReplyKeyboardRemove())
        user_context.pop(user_id, None)
    else:
        bot.send_message(user_id, "Некорректный ввод. Нажмите 1 для удаления или 0 для отмены.")


@bot.message_handler(func=lambda message: (
        str(message.chat.id) in user_context and
        "delete_category" in user_context[str(message.chat.id)]
))
def handle_category_deletion_confirmation(message):
    user_id = str(message.chat.id)
    confirmation = message.text.strip()
    category = user_context[user_id].get("delete_category")

    if not category:
        bot.send_message(user_id, "Ошибка: информация для удаления отсутствует.", reply_markup=ReplyKeyboardRemove())
        return

    if confirmation == "1":  # Подтверждение удаления
        if category in user_categories.get(user_id, {}):
            try:
                # Удаляем категорию с ошибками из errors.json, если такая существует
                if user_id in errors and category in errors[user_id]:
                    del errors[user_id][category]
                    save_json("errors.json", errors)
                # Удаляем категорию из user_categories
                del user_categories[user_id][category]
                save_json("user_categories.json", user_categories)
                bot.send_message(user_id, f"Категория '{category}' успешно удалена.",
                                 reply_markup=ReplyKeyboardRemove())
            except Exception as e:
                bot.send_message(user_id, f"Ошибка при удалении категории: {e}", reply_markup=ReplyKeyboardRemove())
        else:
            bot.send_message(user_id, f"Категория '{category}' не найдена или уже удалена.",
                             reply_markup=ReplyKeyboardRemove())
    elif confirmation == "0":  # Отмена удаления
        bot.send_message(user_id, "Удаление отменено.", reply_markup=ReplyKeyboardRemove())
    else:  # Некорректный ввод
        bot.send_message(user_id, "Некорректный ввод. Нажмите 1 для удаления или 0 для отмены.")

    user_context.pop(user_id, None)


@bot.message_handler(func=lambda message: (
        str(message.chat.id) in user_context and
        "delete_word" in user_context[str(message.chat.id)]
))
def handle_deletion_confirmation(message):
    user_id = str(message.chat.id)
    context = user_context.get(user_id, {})
    confirmation = message.text.strip()

    if confirmation == "1":  # Подтверждение удаления
        delete_info = context.get("delete_word")
        if not delete_info:
            bot.send_message(user_id, "Ошибка: информация для удаления отсутствует.",
                             reply_markup=ReplyKeyboardRemove())
            user_context.pop(user_id, None)  # Удаляем контекст
            return

        category = delete_info["category"]
        question_id = delete_info["question_id"]
        word_to_delete = delete_info["word"]

        # Проверяем существование категории
        if category not in user_categories.get(user_id, {}):
            bot.send_message(user_id, f"Категория '{category}' не найдена или уже удалена.",
                             reply_markup=ReplyKeyboardRemove())
            user_context.pop(user_id, None)  # Удаляем контекст
            return

        # Удаляем слово из категории
        user_categories[user_id][category] = [
            word for word in user_categories[user_id][category]
            if generate_id(word["question"]) != question_id
        ]
        save_json("user_categories.json", user_categories)

        # Подтверждение удаления
        bot.send_message(
            user_id,
            f"Слово '{word_to_delete['question'].replace('←', '/')}' успешно удалено из категории '{category}'.",
            reply_markup=ReplyKeyboardRemove()
        )

        # Удаляем контекст
        user_context.pop(user_id, None)

    elif confirmation == "0":  # Отмена удаления
        bot.send_message(user_id, "Удаление отменено.", reply_markup=ReplyKeyboardRemove())
        user_context.pop(user_id, None)
    else:  # Некорректный ввод
        bot.send_message(user_id, "Некорректный ввод. Нажмите 1 для удаления или 0 для отмены.")


@bot.callback_query_handler(func=lambda call: call.data.startswith("remove_category:"))
def remove_category(call):
    bot.answer_callback_query(call.id)
    user_id = str(call.message.chat.id)
    category = call.data.split(":")[1]

    # Проверяем, существует ли категория
    if category in user_categories.get(user_id, {}):
        # Удаляем связанные ошибки, даже если категория пустая
        if user_id in errors:
            category_questions = {
                word["question"]
                for word in user_categories[user_id].get(category, [])
            }
            errors[user_id] = {
                question: count
                for question, count in errors[user_id].items()
                if question not in category_questions
            }
            save_json("errors.json", errors)

        # Удаляем категорию
        del user_categories[user_id][category]
        save_json("user_categories.json", user_categories)
        bot.send_message(user_id, f"Категория '{category}' успешно удалена.")
    else:
        bot.send_message(user_id, f"Категория '{category}' не найдена.")


@bot.message_handler(commands=['remove_word'])
def remove_word(message):
    """Выбор действия для удаления (категория или слово)."""
    user_id = str(message.chat.id)

    if user_id not in user_categories or not user_categories[user_id]:
        bot.send_message(user_id, "У вас нет категорий или слов для удаления.")
        return

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("Удалить категорию", callback_data="remove_category_menu"))
    markup.add(InlineKeyboardButton("Удалить слово", callback_data="remove_word_menu"))

    bot.send_message(user_id, "Выберите действие для удаления:", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("delete_word:"))
def delete_word(call):
    bot.answer_callback_query(call.id)
    user_id = str(call.message.chat.id)
    # Callback содержит данные в формате "delete_word:category:question_id"
    _, category, question_id = call.data.split(":", 2)
    words = user_categories[user_id].get(category, [])
    # Ищем удаляемый вопрос по хэшу
    word_to_delete = next((word for word in words if generate_id(word["question"]) == question_id), None)
    if not word_to_delete:
        bot.send_message(user_id, "Слово не найдено.")
        return
    # Удаляем слово из категории
    updated_words = [word for word in words if word != word_to_delete]
    user_categories[user_id][category] = updated_words
    save_json("user_categories.json", user_categories)
    # Удаляем связанные ошибки с учетом категории
    if user_id in errors and category in errors[user_id]:
        errors[user_id][category].pop(word_to_delete["question"], None)
        if not errors[user_id][category]:
            del errors[user_id][category]
        save_json("errors.json", errors)
    bot.send_message(user_id, f"Слово '{word_to_delete['question']}' удалено из категории '{category}'.")


# Напоминание-викторина
def send_daily_quiz():
    for user_id in user_categories:
        categories = user_categories[user_id]
        if not categories:
            continue  # У пользователя нет категорий

        # Выбираем случайную категорию и случайное слово
        category_name = random.choice(list(categories.keys()))
        words = categories[category_name]
        if not words:
            continue  # Категория пуста

        question_data = random.choice(words)
        question, correct_answer = question_data["question"].split("←")
        options = [question.split("←")[0], correct_answer]
        random.shuffle(options)

        # Создаем клавиатуру с вариантами ответа
        markup = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        markup.add(KeyboardButton(options[0]), KeyboardButton(options[1]))

        bot.send_message(
            user_id,
            f"🕰️ Время викторины!\nКатегория: {category_name}\nВыберите правильный ответ:",
            reply_markup=markup
        )

        # Сохраняем текущий вопрос в контексте пользователя
        if user_id not in user_context:
            user_context[user_id] = {}
        user_context[user_id]["current_quiz"] = {
            "correct": correct_answer,
            "question": question,
        }


# Проверка ответа на викторину
@bot.message_handler(
    func=lambda message: str(message.chat.id) in user_context and "current_quiz" in user_context[str(message.chat.id)]
)
def handle_quiz_answer(message):
    user_id = str(message.chat.id)
    user_answer = message.text.strip()
    quiz_data = user_context[user_id]["current_quiz"]
    correct_answer = quiz_data["correct"]
    question_text = quiz_data["question"]
    category = quiz_data["category"]

    if user_answer == correct_answer:
        bot.send_message(user_id, "✅ Верно!")
        # Уменьшаем количество ошибок: если становится 0 или меньше – удаляем слово из ошибок
        current_count = errors.get(user_id, {}).get(category, {}).get(question_text, 0)
        new_count = current_count - 1
        if new_count <= 0:
            if user_id in errors and category in errors[user_id]:
                errors[user_id][category].pop(question_text, None)
                if not errors[user_id][category]:
                    del errors[user_id][category]
        else:
            errors.setdefault(user_id, {}).setdefault(category, {})[question_text] = new_count
    else:
        bot.send_message(user_id, f"❌ Неверно! Правильный ответ: {correct_answer}.")
        # При неверном ответе увеличиваем количество ошибок на 1
        current_count = errors.get(user_id, {}).get(category, {}).get(question_text, 0)
        new_count = current_count + 1
        errors.setdefault(user_id, {}).setdefault(category, {})[question_text] = new_count

    save_json("errors.json", errors)
    # Удаляем текущую викторину из контекста
    del user_context[user_id]["current_quiz"]



# Словарь для хранения времени отправки викторины для каждого пользователя
quiz_schedule = load_json("quiz_schedule.json", {})


# Команда /quiz для настройки времени викторины
@bot.message_handler(commands=['quiz'])
def set_quiz_time(message):
    user_id = str(message.chat.id)

    # Проверяем, есть ли уже сохранённые времена для пользователя
    user_times = quiz_schedule.get(user_id, [])
    if user_times:
        # Сортируем времена в порядке возрастания
        sorted_times = sorted(user_times, key=lambda t: list(map(int, t.split(":"))))

        # Формируем список времён в удобном формате
        times_list = "\n".join([f"- {time}" for time in sorted_times])
        bot.send_message(
            user_id,
            f"Ваши текущие времена для викторины:\n{times_list}\n\n"
            "Введите новое время в формате ЧЧ:ММ для добавления, "
            "или введите существующее время для удаления. Для отмены введите 0. Время вводится по времени МСК+2"
        )
    else:
        bot.send_message(
            user_id,
            "У вас пока нет сохранённых времён. Введите время в формате ЧЧ:ММ для добавления. Для отмены введите 0."
        )

    # Устанавливаем контекст для обработки времени
    user_context[user_id] = {"mode": "set_quiz_time"}


import re


@bot.message_handler(func=lambda message: user_context.get(str(message.chat.id), {}).get("mode") == "set_quiz_time")
def handle_quiz_time_input(message):
    user_id = str(message.chat.id)
    time_input = message.text.strip()

    # Отмена операции
    if time_input == "0":
        bot.send_message(user_id, "Операция отменена.")
        user_context.pop(user_id, None)
        return

    # Проверка формата времени (ЧЧ:ММ)
    if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", time_input):
        bot.send_message(user_id, "Ошибка! Введите время в правильном формате ЧЧ:ММ (например, 01:52).")
        return

    # Удаление существующего времени
    if time_input in quiz_schedule.get(user_id, []):
        quiz_schedule[user_id].remove(time_input)
        save_json("quiz_schedule.json", quiz_schedule)
        bot.send_message(user_id, f"Время {time_input} удалено из расписания.")
        user_context.pop(user_id, None)
        return

    # Добавление нового времени
    quiz_schedule.setdefault(user_id, []).append(time_input)
    save_json("quiz_schedule.json", quiz_schedule)

    bot.send_message(user_id, f"Время {time_input} добавлено в расписание.")
    user_context.pop(user_id, None)  # Убираем режим настройки


def send_scheduled_quizzes():
    current_time = time.strftime("%H:%M")
    for user_id, times in quiz_schedule.items():
        if current_time in times:
            # Получаем ошибки для пользователя из errors.json
            user_errors = errors.get(user_id, {})
            words_list = []
            for category, qdict in user_errors.items():
                for question, count in qdict.items():
                    words_list.append((category, question, count))
            if not words_list:
                continue

            # Выбор слова с учётом веса (больше ошибок – выше шанс)
            chosen = random.choices(words_list, weights=[item[2] for item in words_list])[0]
            category_name, question_text, _ = chosen

            try:
                wrong, correct = question_text.split("←")
            except Exception:
                continue

            options = [wrong.strip(), correct.strip()]
            random.shuffle(options)
            markup = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
            markup.add(KeyboardButton(options[0]), KeyboardButton(options[1]))

            # Если ранее было отправлено сообщение викторины, удаляем его, чтобы убрать старую клавиатуру
            if user_id in user_context and "last_quiz_msg_id" in user_context[user_id]:
                try:
                    bot.delete_message(user_id, user_context[user_id]["last_quiz_msg_id"])
                except Exception:
                    pass

            # Отправляем новое сообщение викторины с нужной клавиатурой
            msg = bot.send_message(
                user_id,
                f"🕰️ Время викторины!\nКатегория: {category_name}\nВыберите правильный ответ:",
                reply_markup=markup
            )

            # Сохраняем id нового сообщения для последующего удаления
            user_context.setdefault(user_id, {})["last_quiz_msg_id"] = msg.message_id

            # Сохраняем данные текущей викторины в контексте пользователя
            user_context[user_id]["current_quiz"] = {
                "correct": correct.strip(),
                "question": question_text,
                "category": category_name
            }


# Планировщик для отправки викторин утром и вечером
def schedule_quiz():
    schedule.every(1).minutes.do(send_scheduled_quizzes)  # Проверяем каждую минуту

    while True:
        schedule.run_pending()
        time.sleep(1)


# Запускаем планировщик в отдельном потоке
threading.Thread(target=schedule_quiz, daemon=True).start()


def generate_callback_data(data):
    """Создаёт короткий уникальный идентификатор для callback_data."""
    return hashlib.md5(data.encode('utf-8')).hexdigest()[:8]


@bot.message_handler(commands=['start_global'])
def start_global(message):
    user_id = str(message.chat.id)
    questions = []

    # Сбор всех вопросов из categories_for_all_users.json
    for category, question_list in categories_for_all_users.items():
        questions.extend(question_list)

    if not questions:
        bot.send_message(user_id, "Нет доступных вопросов для игры.")
        return

    # Перемешиваем вопросы
    random.shuffle(questions)

    # Сохраняем контекст игры
    user_context[user_id] = {
        "mode": "global_game",
        "questions": questions,
        "current": None
    }

    bot.send_message(user_id, "Глобальная игра началась! Отвечайте на вопросы.")
    send_global_question(user_id)


def send_global_question(user_id):
    context = user_context.get(user_id)

    if not context or not context["questions"]:
        bot.send_message(user_id, "Вы ответили на все доступные вопросы!")
        user_context.pop(user_id, None)
        return

    # Извлекаем вопрос
    question = context["questions"].pop(0)
    context["current"] = question

    # Формируем кнопки с вариантами ответа
    btn1, btn2 = question["question"].split("←")
    options = [btn1, btn2]
    random.shuffle(options)

    markup = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.add(KeyboardButton(options[0]), KeyboardButton(options[1]))

    bot.send_message(user_id, "Выберите ответ:", reply_markup=markup)


@bot.message_handler(func=lambda message: user_context.get(str(message.chat.id), {}).get("mode") == "global_game")
def handle_global_answer(message):
    user_id = str(message.chat.id)
    user_answer = message.text.strip()
    context = user_context[user_id]

    # Проверяем текущий вопрос
    current_question = context["current"]
    correct_answer = current_question["correct"].strip()

    if user_answer == correct_answer:
        bot.send_message(user_id, "✅ Верно!")
        send_global_question(user_id)
    else:
        bot.send_message(user_id, f"❌ Неверно! Правильный ответ: {correct_answer}.", reply_markup=ReplyKeyboardRemove())
        user_context.pop(user_id, None)  # Завершаем игру после неверного ответа


ERRORS_PER_PAGE = 10  # Количество ошибок на одной странице


@bot.message_handler(commands=['clean_error'])
def clean_error(message):
    user_id = str(message.chat.id)
    if user_id not in errors or not errors[user_id]:
        bot.send_message(user_id, "У вас нет ошибок для очистки!")
        return

    # Сортировка категорий по числовому префиксу "№", если он присутствует
    def sort_key(category):
        match = re.search(r'№\s*(\d+)', category)
        return int(match.group(1)) if match else float('inf')

    sorted_categories = sorted(errors[user_id].keys(), key=sort_key)

    markup = InlineKeyboardMarkup()
    for category in sorted_categories:
        markup.add(InlineKeyboardButton(category, callback_data=f"clean_cat:{category}"))
    bot.send_message(user_id, "Выберите категорию ошибок для очистки:", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("clean_cat:"))
def clean_cat_handler(call):
    bot.answer_callback_query(call.id)
    user_id = str(call.message.chat.id)
    category = call.data.split(":", 1)[1]
    if user_id not in errors or category not in errors[user_id]:
        bot.send_message(user_id, "Ошибки в выбранной категории не найдены.")
        return

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("Удалить все ошибки", callback_data=f"clean_all:{category}"))
    markup.add(InlineKeyboardButton("Выбрать ошибки для удаления", callback_data=f"clean_select:{category}"))
    markup.add(InlineKeyboardButton("Отмена", callback_data="clean_cancel"))

    try:
        bot.edit_message_text(f"Вы выбрали категорию '{category}'. Что хотите сделать?",
                              chat_id=user_id, message_id=call.message.message_id, reply_markup=markup)
    except Exception as e:
        bot.send_message(user_id, f"Ошибка при обновлении сообщения: {e}")


@bot.callback_query_handler(func=lambda call: call.data.startswith("clean_all:"))
def clean_all_handler(call):
    bot.answer_callback_query(call.id)
    user_id = str(call.message.chat.id)
    category = call.data.split(":", 1)[1]
    if user_id in errors and category in errors[user_id]:
        del errors[user_id][category]
        save_json("errors.json", errors)
        bot.edit_message_text(f"Все ошибки из категории '{category}' удалены.",
                              chat_id=user_id, message_id=call.message.message_id)
    else:
        bot.send_message(user_id, "Ошибки не найдены.")


@bot.callback_query_handler(func=lambda call: call.data.startswith("clean_select:"))
def clean_select_handler(call):
    bot.answer_callback_query(call.id)
    user_id = str(call.message.chat.id)
    category = call.data.split(":", 1)[1]
    if user_id not in errors or category not in errors[user_id]:
        bot.send_message(user_id, "Ошибки в выбранной категории не найдены.")
        return

    markup = InlineKeyboardMarkup()
    for question, count in errors[user_id][category].items():
        # Если в вопросе есть "←", берем правую часть (правильный вариант)
        correct_part = question.split("←")[1] if "←" in question else question
        btn_text = f"{correct_part} ({count})"
        qhash = generate_id(question)
        markup.add(InlineKeyboardButton(btn_text, callback_data=f"clean_one:{category}:{qhash}"))
    markup.add(InlineKeyboardButton("Готово", callback_data="clean_select_done"))
    try:
        bot.edit_message_text(f"Выберите ошибки для удаления в категории '{category}':",
                              chat_id=user_id, message_id=call.message.message_id, reply_markup=markup)
    except Exception as e:
        bot.send_message(user_id, f"Ошибка при обновлении сообщения: {e}")


@bot.callback_query_handler(func=lambda call: call.data.startswith("clean_one:"))
def clean_one_handler(call):
    bot.answer_callback_query(call.id)
    user_id = str(call.message.chat.id)
    parts = call.data.split(":")
    if len(parts) < 3:
        bot.answer_callback_query(call.id, "Неверные данные.")
        return
    category = parts[1]
    qhash = parts[2]
    if user_id not in errors or category not in errors[user_id]:
        bot.answer_callback_query(call.id, "Ошибки не найдены.")
        return

    error_found = None
    for question in list(errors[user_id][category].keys()):
        if generate_id(question) == qhash:
            error_found = question
            break

    if error_found:
        del errors[user_id][category][error_found]
        # Если в категории не осталось ошибок, удаляем категорию
        if not errors[user_id][category]:
            del errors[user_id][category]
        save_json("errors.json", errors)
        bot.answer_callback_query(call.id, "Ошибка удалена.")
        # Обновляем клавиатуру с оставшимися ошибками
        markup = InlineKeyboardMarkup()
        if user_id in errors and category in errors[user_id]:
            for question, count in errors[user_id][category].items():
                correct_part = question.split("←")[1] if "←" in question else question
                btn_text = f"{correct_part} ({count})"
                qh = generate_id(question)
                markup.add(InlineKeyboardButton(btn_text, callback_data=f"clean_one:{category}:{qh}"))
        markup.add(InlineKeyboardButton("Готово", callback_data="clean_select_done"))
        try:
            bot.edit_message_text(f"Выберите ошибки для удаления в категории '{category}':",
                                  chat_id=user_id, message_id=call.message.message_id, reply_markup=markup)
        except Exception as e:
            bot.send_message(user_id, f"Ошибка при обновлении сообщения: {e}")
    else:
        bot.answer_callback_query(call.id, "Ошибка не найдена.")


@bot.callback_query_handler(func=lambda call: call.data == "clean_select_done")
def clean_select_done_handler(call):
    user_id = str(call.message.chat.id)
    try:
        bot.edit_message_text("Очистка ошибок завершена.", chat_id=user_id, message_id=call.message.message_id)
    except Exception as e:
        bot.send_message(user_id, f"Ошибка при обновлении сообщения: {e}")
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "clean_cancel")
def clean_cancel_handler(call):
    user_id = str(call.message.chat.id)
    try:
        bot.edit_message_text("Очистка ошибок отменена.", chat_id=user_id, message_id=call.message.message_id)
    except Exception as e:
        bot.send_message(user_id, f"Ошибка при обновлении сообщения: {e}")
    bot.answer_callback_query(call.id)


def send_error_list(user_id):
    """Отправляет список ошибок с пагинацией."""
    if user_id not in user_context or "error_list" not in user_context[user_id]:
        bot.send_message(user_id, "Ошибка: список ошибок устарел, попробуйте снова.")
        return

    errors_list = user_context[user_id]["error_list"]
    page = user_context[user_id]["current_page"]

    total_pages = (len(errors_list) - 1) // ERRORS_PER_PAGE + 1  # Количество страниц
    start = page * ERRORS_PER_PAGE
    end = start + ERRORS_PER_PAGE

    markup = InlineKeyboardMarkup()
    error_map = {}

    for error, count in errors_list[start:end]:
        error_text = error.replace("←", "/")  # Заменяем ← на /
        error_hash = generate_hash(error)  # Генерируем хэш вместо полного текста
        error_map[error_hash] = error
        markup.add(InlineKeyboardButton(f"{error_text} ({count})", callback_data=f"clean_error:{error_hash}"))

    # Кнопки "⏪ Назад" и "⏩ Далее"
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⏪ Назад", callback_data="prev_error_page"))
    if end < len(errors_list):
        nav_buttons.append(InlineKeyboardButton("⏩ Далее", callback_data="next_error_page"))

    if nav_buttons:
        markup.row(*nav_buttons)  # Добавляем кнопки в одну строку

    user_context[user_id]["error_map"] = error_map  # Сохраняем ошибки в контексте
    bot.send_message(user_id, f"📋 Ошибки (страница {page + 1} из {total_pages}):", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data in ["prev_error_page", "next_error_page"])
def paginate_errors(call):
    """Переключает страницы списка ошибок."""
    bot.answer_callback_query(call.id)
    user_id = str(call.message.chat.id)

    if user_id not in user_context or "current_page" not in user_context[user_id]:
        bot.send_message(user_id, "Ошибка: кеш данных устарел, попробуйте снова.")
        return

    if call.data == "prev_error_page":
        user_context[user_id]["current_page"] -= 1
    elif call.data == "next_error_page":
        user_context[user_id]["current_page"] += 1

    send_error_list(user_id)  # Отправляем обновленный список ошибок


@bot.callback_query_handler(func=lambda call: call.data.startswith("clean_error:"))
def handle_clean_error(call):
    bot.answer_callback_query(call.id)
    user_id = str(call.message.chat.id)

    # Извлекаем хэш ошибки
    error_hash = call.data.split(":", 1)[1]
    error_map = user_context.get(user_id, {}).get("error_map", {})
    error_key = error_map.get(error_hash)

    # Проверяем, существует ли ошибка
    if not error_key or error_key not in errors.get(user_id, {}):
        bot.send_message(user_id, "Ошибка не найдена или уже удалена.")
        return

    count = errors[user_id][error_key]
    bot.send_message(
        user_id,
        f"Вы выбрали ошибку: {error_key} (количество: {count}).\n"
        "Введите:\n"
        "- `0` для полного удаления ошибки;\n"
        "- `число` на которое нужно изменить (например, 2).\n"
        "Отправьте новое значение:".replace('←', '/'),
        parse_mode="Markdown"
    )
    user_context[user_id]["clean_error"] = error_key  # Сохраняем текущую ошибку


def generate_hash(data):
    """Генерирует короткий хэш для данных"""
    return hashlib.md5(data.encode('utf-8')).hexdigest()[:8]


@bot.message_handler(
    func=lambda message: str(message.chat.id) in user_context and "clean_error" in user_context[str(message.chat.id)])
def handle_clean_error_input(message):
    user_id = str(message.chat.id)
    context = user_context[user_id]

    # Получаем текущую ошибку, которую пользователь хочет изменить
    error_key = context.get("clean_error")
    if not error_key:
        bot.send_message(user_id, "Контекст ошибки не найден. Попробуйте заново выбрать ошибку с помощью /clean_error.")
        return

    try:
        # Парсим пользовательский ввод
        input_value = int(message.text.strip())
        if input_value < 0:
            raise ValueError("Отрицательное значение не допускается!")

        elif input_value == 0:
            # Удаляем ошибку полностью
            del errors[user_id][error_key]
            bot.send_message(user_id, f"Ошибка '{error_key}' полностью удалена.".replace('←', '/'))
        else:
            # Уменьшаем количество ошибок
            current_count = errors[user_id].get(error_key, 0)
            new_count = input_value
            if new_count >= current_count:
                bot.send_message(user_id, f"Значение больше исходного не допускается!")
            elif 0 < new_count < current_count:
                errors[user_id][error_key] = new_count
                bot.send_message(user_id,
                                 f"Количество для ошибки '{error_key}' обновлено: {new_count}.".replace('←', '/'))

        # Сохраняем обновления
        save_json("errors.json", errors)

    except ValueError:
        bot.send_message(user_id, "Некорректное значение. Введите число 0 или больше.")
    finally:
        # Удаляем контекст для этой команды
        user_context[user_id].pop("clean_error", None)


# ========== Обработка команды /change_word ==========
@bot.message_handler(commands=['change_word'])
def change_word(message):
    user_id = str(message.chat.id)
    categories = sorted(user_categories.get(user_id, {}).keys(), key=natural_sort_key)

    if not categories:
        bot.send_message(user_id, "⚠ У вас нет категорий для изменения.")
        return

    markup = InlineKeyboardMarkup()
    for category in categories:
        category_hash = generate_category_hash(category)
        markup.add(InlineKeyboardButton(
            f"{category}",
            callback_data=f"search_word_change:{category_hash}"
        ))

    msg = bot.send_message(user_id, "📚 Выберите категорию для изменения слов:", reply_markup=markup)

    user_context[user_id] = {
        "action": "change_word_init",
        "category_hash_map": {generate_category_hash(c): c for c in categories},
        "message_id": msg.message_id
    }


# ========== Обработка выбора категории ==========
@bot.callback_query_handler(func=lambda call: call.data.startswith(SEARCH_CHANGE_PREFIX))
def handle_search_word_change(call):
    user_id = str(call.message.chat.id)
    category_hash = call.data.split(":", 1)[1]

    if user_id not in user_context or "category_hash_map" not in user_context[user_id]:
        bot.send_message(user_id, "⚠ Сессия устарела. Начните заново.")
        return

    category_name = user_context[user_id]["category_hash_map"].get(category_hash)
    if not category_name:
        bot.send_message(user_id, "⚠ Категория не найдена.")
        return

    # Обновляем контекст
    user_context[user_id].update({
        "action": "search_word_change",
        "current_category": category_name,
        "category_hash": category_hash,
        "current_page": 0
    })

    bot.send_message(user_id, f"🔍 Введите часть слова для поиска в категории '{category_name}':")
    bot.answer_callback_query(call.id)


# ========== Поиск слов для изменения ==========
@bot.message_handler(func=lambda message:
str(message.chat.id) in user_context and
user_context[str(message.chat.id)].get("action") == "search_word_change")
def handle_search_word_change_input(message):
    user_id = str(message.chat.id)
    search_query = message.text.strip().lower()
    context = user_context[user_id]

    category_name = context["current_category"]
    words = user_categories[user_id].get(category_name, [])

    # Фильтрация слов
    filtered_words = [
        word for word in words
        if search_query in word["question"].lower()
    ]

    if not filtered_words:
        bot.send_message(user_id, f"❌ Слова с '{search_query}' не найдены.")
        user_context.pop(user_id, None)
        return

    # Сохраняем результаты поиска
    context.update({
        "action": "select_word_to_edit",
        "filtered_words": filtered_words,
        "total_pages": (len(filtered_words) - 1) // WORDS_PER_PAGE + 1
    })

    send_edit_word_list(user_id)


# ========== Отправка списка слов для редактирования ==========
def send_edit_word_list(user_id, page=0):
    context = user_context.get(user_id)
    if not context or "filtered_words" not in context:
        bot.send_message(user_id, "⚠ Сессия устарела. Начните заново.")
        return

    words = context["filtered_words"]
    start = page * WORDS_PER_PAGE
    end = start + WORDS_PER_PAGE
    current_page = page + 1

    markup = InlineKeyboardMarkup()
    for word in words[start:end]:
        question_hash = generate_id(word["question"])
        btn_text = word["question"].replace("←", " / ")[:30]  # Обрезаем длинные названия
        markup.add(InlineKeyboardButton(
            f"{btn_text}",
            callback_data=f"{EDIT_WORD_PREFIX}{question_hash}"
        ))

    # Добавляем пагинацию
    if len(words) > WORDS_PER_PAGE:
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("⬅ Назад", callback_data=f"edit_prev:{page - 1}"))
        if end < len(words):
            nav_buttons.append(InlineKeyboardButton("Вперед ➡", callback_data=f"edit_next:{page + 1}"))
        markup.row(*nav_buttons)

    # Добавляем кнопку отмены
    markup.add(InlineKeyboardButton("❌ Отменить", callback_data="edit_cancel"))

    bot.send_message(
        user_id,
        f"📝 Найдено слов: {len(words)}\nСтраница {current_page}/{context['total_pages']}",
        reply_markup=markup
    )


# ========== Обработка пагинации ==========
@bot.callback_query_handler(func=lambda call: call.data.startswith(("edit_prev:", "edit_next:")))
def handle_edit_pagination(call):
    user_id = str(call.message.chat.id)
    action, page = call.data.split(":")
    page = int(page)

    if user_id not in user_context or "filtered_words" not in user_context[user_id]:
        bot.answer_callback_query(call.id, "⚠ Сессия устарела")
        return

    send_edit_word_list(user_id, page)
    bot.answer_callback_query(call.id)


# ========== Обработка выбора слова ==========
@bot.callback_query_handler(func=lambda call: call.data.startswith(EDIT_WORD_PREFIX))
def handle_edit_word_selection(call):
    user_id = str(call.message.chat.id)
    question_hash = call.data.split(":", 1)[1]

    if user_id not in user_context or "filtered_words" not in user_context[user_id]:
        bot.answer_callback_query(call.id, "⚠ Сессия устарела")
        return

    # Поиск выбранного слова
    selected_word = next(
        (word for word in user_context[user_id]["filtered_words"]
         if generate_id(word["question"]) == question_hash),
        None
    )

    if not selected_word:
        bot.answer_callback_query(call.id, "⚠ Слово не найдено")
        return

    # Сохраняем данные для редактирования
    user_context[user_id].update({
        "action": "edit_word_input",
        "selected_word": selected_word,
        "original_question": selected_word["question"]
    })

    # Отправляем инструкцию
    bot.send_message(
        user_id,
        "✍️ Введите новую пару слов в формате:\n"
        "❌ Неверный вариант (первая строка)\n"
        "✅ Верный вариант (вторая строка)\n\n"
        "Пример:\n"
        "Неправильноенаписание\n"
        "Правильное написание",
        reply_markup=ReplyKeyboardRemove()
    )
    bot.answer_callback_query(call.id)


# ========== Обработка ввода новых данных ==========
@bot.message_handler(func=lambda message:
str(message.chat.id) in user_context and
user_context[str(message.chat.id)].get("action") == "edit_word_input")
def handle_edit_word_input(message):
    user_id = str(message.chat.id)
    context = user_context[user_id]

    try:
        wrong, correct = message.text.strip().split("\n", 1)
        wrong = wrong.strip()
        correct = correct.strip()

        # Валидация
        if not wrong or not correct:
            raise ValueError("Пустые строки")
        if len(wrong) > 50 or len(correct) > 50:
            raise ValueError("Слишком длинные варианты (>50 символов)")
        if "←" in wrong:
            raise ValueError("Недопустимый символ ← в вариантах")

        # Обновляем слово
        category_name = context["current_category"]
        new_question = f"{wrong}←{correct}"

        # Находим и заменяем в user_categories
        for idx, word in enumerate(user_categories[user_id][category_name]):
            if word["question"] == context["original_question"]:
                user_categories[user_id][category_name][idx] = {
                    "question": new_question,
                    "correct": correct
                }
                break

        # Обновляем ошибки
        if user_id in errors:
            if context["original_question"] in errors[user_id]:
                errors[user_id][new_question] = errors[user_id].pop(context["original_question"])

        save_json("user_categories.json", user_categories)
        save_json("errors.json", errors)

        bot.send_message(user_id, "✅ Слово успешно обновлено!")

    except Exception as e:
        error_msg = {
            "ValueError": f"❌ Ошибка формата: {e}",
            "IndexError": "❌ Нужно ввести ДВЕ строки!",
        }.get(type(e).__name__, "❌ Неизвестная ошибка")

        bot.send_message(user_id, error_msg + "\nПопробуйте еще раз:")
        return

    finally:
        user_context.pop(user_id, None)


# ========== Обработка отмены ==========
@bot.callback_query_handler(func=lambda call: call.data == "edit_cancel")
def handle_edit_cancel(call):
    user_id = str(call.message.chat.id)
    user_context.pop(user_id, None)
    bot.send_message(user_id, "❌ Изменение отменено.")
    bot.answer_callback_query(call.id)


# ========== Защита от повторных нажатий ==========
@bot.callback_query_handler(func=lambda call: call.data.startswith(EDIT_WORD_PREFIX))
def handle_expired_edit(call):
    bot.answer_callback_query(call.id, "⚠ Это действие больше не актуально", show_alert=True)


@bot.callback_query_handler(func=lambda call: call.data.startswith("search_word_change:"))
def ask_for_search_word_change(call):
    """Запрашиваем у пользователя слово для поиска перед изменением."""
    bot.answer_callback_query(call.id)
    user_id = str(call.message.chat.id)
    category_hash = call.data.split(":")[1]

    if user_id not in user_context or "category_hash_map" not in user_context[user_id]:
        bot.send_message(user_id, "⚠ Данные устарели. Выберите категорию заново.")
        return change_word(call.message)

    category_name = user_context[user_id]["category_hash_map"].get(category_hash)
    if not category_name:
        bot.send_message(user_id, "⚠ Ошибка: категория не найдена. Выберите заново.")
        return change_word(call.message)

    user_context[user_id]["category_hash"] = category_hash
    user_context[user_id]["search_mode"] = True

    bot.send_message(user_id, f"🔎 Введите часть слова, которое хотите изменить в категории '{category_name}':")


@bot.message_handler(
    func=lambda message: str(message.chat.id) in user_context and user_context[str(message.chat.id)].get("search_mode"))
def search_word_to_change(message):
    """Фильтруем слова в категории по введенному запросу перед изменением."""
    user_id = str(message.chat.id)
    search_query = message.text.strip().lower()

    category_hash = user_context[user_id].get("category_hash")
    if not category_hash or "category_hash_map" not in user_context[user_id]:
        bot.send_message(user_id, "⚠ Данные устарели. Выберите категорию заново.")
        return change_word(message)

    category_name = user_context[user_id]["category_hash_map"].get(category_hash)
    if not category_name or category_name not in user_categories.get(user_id, {}):
        bot.send_message(user_id, "⚠ Ошибка: категория не найдена.")
        return change_word(message)

    words = user_categories[user_id][category_name]
    filtered_words = [word for word in words if search_query in word["question"].lower()]

    if not filtered_words:
        bot.send_message(user_id,
                         f"❌ В категории '{category_name}' не найдено слов, содержащих '{search_query}'. Попробуйте снова.")
        return

    user_context[user_id]["word_list"] = filtered_words
    user_context[user_id]["search_mode"] = False

    send_change_word_list(user_id)


WORDS_PER_PAGE = 10  # Количество слов на странице


def send_change_word_list(user_id):
    if user_id not in user_context or "word_list" not in user_context[user_id]:
        bot.send_message(user_id, "⚠ Ошибка: кеш данных устарел, попробуйте снова.")
        return

    words = user_context[user_id]["word_list"]
    category_hash = user_context[user_id]["category_hash"]

    markup = InlineKeyboardMarkup()
    word_hash_map = {}

    for word in words:
        question_text = word["question"].replace("←", " / ")
        question_hash = generate_id(word["question"])
        word_hash_map[question_hash] = word["question"]
        # Исправлено: используем edit_word вместо confirm_remove_word
        markup.add(InlineKeyboardButton(f"✏ Изменить: {question_text}",
                                        callback_data=f"edit_word:{category_hash}:{question_hash}"))

    user_context[user_id]["word_hash_map"] = word_hash_map
    bot.send_message(user_id, "📖 Найденные слова. Выберите слово для **изменения**:", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data in ["prev_word_page", "next_word_page"])
def paginate_words_change(call):
    """Переключает страницы списка слов перед изменением."""
    bot.answer_callback_query(call.id)
    user_id = str(call.message.chat.id)

    if user_id not in user_context or "current_page" not in user_context[user_id]:
        bot.send_message(user_id, "Ошибка: кеш данных устарел, попробуйте снова.")
        return

    if call.data == "prev_word_page":
        user_context[user_id]["current_page"] -= 1
    elif call.data == "next_word_page":
        user_context[user_id]["current_page"] += 1

    send_change_word_list(user_id)  # Отправляем обновленный список слов


@bot.callback_query_handler(func=lambda call: call.data.startswith("change_category:"))
def handle_change_category(call):
    bot.answer_callback_query(call.id)
    user_id = str(call.message.chat.id)
    category = call.data.split(":")[1]
    bot.send_message(user_id, f"Введите новое название для категории '{category}':")
    user_context[user_id] = {"action": "change_category", "category": category}


@bot.callback_query_handler(func=lambda call: call.data.startswith("change_word:"))
def handle_change_word_in_category(call):
    """Обрабатывает выбор категории для изменения слова."""
    bot.answer_callback_query(call.id)
    user_id = str(call.message.chat.id)
    category_hash = call.data.split(":")[1]

    # Проверяем, есть ли контекст и загружены ли категории
    if user_id not in user_context or "category_hash_map" not in user_context[user_id]:
        bot.send_message(user_id, "⚠ Данные устарели. Выберите категорию заново.", reply_markup=ReplyKeyboardRemove())
        return change_word(call.message)  # Перенаправляем к выбору категории

    category_name = user_context[user_id]["category_hash_map"].get(category_hash)
    if not category_name:
        bot.send_message(user_id, "Ошибка: категория не найдена. Выберите заново.")
        return change_word(call.message)

    words = user_categories[user_id].get(category_name, [])
    if not words:
        bot.send_message(user_id, f"В категории '{category_name}' нет слов для изменения.")
        return

    markup = InlineKeyboardMarkup()
    word_hash_map = {}

    for word in words:
        word_display = word["question"].replace('←', '/')  # Показываем текст вопроса
        word_hash = generate_id(word["question"])  # Генерируем короткий хэш
        word_hash_map[word_hash] = word["question"]  # Сохраняем соответствие

        markup.add(InlineKeyboardButton(word_display, callback_data=f"edit_word:{category_hash}:{word_hash}"))

    user_context[user_id]["word_hash_map"] = word_hash_map  # Сохраняем хэши слов
    bot.send_message(user_id, f"Выберите слово для изменения в категории '{category_name}':", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("edit_word:"))
def handle_edit_word(call):
    """Обрабатывает выбор слова для редактирования."""
    bot.answer_callback_query(call.id)
    user_id = str(call.message.chat.id)
    _, category_hash, word_hash = call.data.split(":")

    if user_id not in user_context or "category_hash_map" not in user_context[user_id]:
        bot.send_message(user_id, "⚠ Данные устарели. Выберите категорию заново.")
        return change_word(call.message)

    category_name = user_context[user_id]["category_hash_map"].get(category_hash)
    if not category_name:
        bot.send_message(user_id, "⚠ Ошибка: категория не найдена.")
        return change_word(call.message)

    word_question = user_context[user_id]["word_hash_map"].get(word_hash)
    if not word_question:
        bot.send_message(user_id, "⚠ Ошибка: слово не найдено.")
        return send_change_word_list(user_id)

    bot.send_message(
        user_id,
        "✏ Введите новое слово в **таком формате**:\n"
        "❌ Неверный вариант (первая строка)\n"
        "✅ Верный вариант (вторая строка):",
        parse_mode="Markdown"
    )

    user_context[user_id] = {
        "action": "edit_word",
        "category": category_name,
        "word_hash": word_hash
    }


@bot.message_handler(
    func=lambda message: (
            str(message.chat.id) in user_context
            and user_context[str(message.chat.id)].get("action") == "edit_word"
    )
)
def handle_change_word_input(message):
    user_id = str(message.chat.id)
    context = user_context[user_id]
    action = context.get("action")

    if action == "edit_word":
        word_hash = context.get("word_hash")
        if not word_hash:
            bot.send_message(user_id, "Ошибка: данные устарели. Попробуйте заново выбрать слово через /change_word.")
            user_context.pop(user_id, None)
            return

        new_word_data = message.text.strip().split("\n", 1)
        if len(new_word_data) != 2:
            bot.send_message(
                user_id,
                "❌ Ошибка! Введите слово в **таком формате**:\n"
                "❌ Неверный вариант (первая строка)\n"
                "✅ Верный вариант (вторая строка):",
                parse_mode="Markdown"
            )
            return

        category = context["category"]
        words = user_categories[user_id][category]

        word = next((w for w in words if generate_id(w["question"]) == word_hash), None)

        if word:
            old_question = word["question"]
            word["question"] = f"{new_word_data[0].strip()}←{new_word_data[1].strip()}"
            word["correct"] = new_word_data[1].strip()

            if user_id in errors and old_question in errors[user_id]:
                error_count = errors[user_id].pop(old_question)
                errors[user_id][word["question"]] = error_count
                save_json("errors.json", errors)

            save_json("user_categories.json", user_categories)
            bot.send_message(user_id, "✅ **Слово успешно изменено!**", parse_mode="Markdown")
        else:
            bot.send_message(user_id, "Ошибка: слово не найдено.")

    user_context.pop(user_id, None)


@bot.callback_query_handler(func=lambda call: True)
def handle_stale_callbacks(call):
    user_id = str(call.message.chat.id)
    if not user_context.get(user_id):
        bot.answer_callback_query(
            call.id,
            "⚠️ Сессия устарела. Начните заново.",
            show_alert=True
        )
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass


def cleanup_context():
    now = time.time()
    for user_id in list(user_context.keys()):
        if now - user_context[user_id].get("timestamp", 0) > 300:  # 5 минут
            del user_context[user_id]


def context_cleaner():
    while True:
        cleanup_context()
        time.sleep(60)

while True:
     try:
         bot.polling(none_stop=True, timeout=60)
     except Exception as e:
         # print(f"Ошибка polling: {e}. Перезапуск через 5 секунд...")
         time.sleep(5)

# Запуск бота
bot.polling()
