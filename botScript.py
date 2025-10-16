import logging
import os
import sys
try:
    from dotenv import load_dotenv
except Exception:
    # dotenv not installed; we'll still allow reading os.environ
    load_dotenv = None

from telegram import Update, InputFile, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, MessageHandler, CallbackContext, CallbackQueryHandler, Filters
from telegram import error as tg_error
from uuid import uuid4

# In-memory store for pending approvals: approval_id -> {file_id, caption, poll}
APPROVALS = {}

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# Load configuration from .env or environment variables
if load_dotenv:
    load_dotenv()

# Define the channel ID where images will be forwarded
CHANNEL_ID = os.environ.get('CHANNEL_ID', 'your_channel_id_here')
OWNER_CHAT_ID = os.environ.get('OWNER_CHAT_ID', 'owner_chat_id')

TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE')

# Full guide message to show when asking for caption or poll
GUIDE_TEXT = (
    "Send me the caption or poll.\n\n"
    "To add a caption:\n"
    "- Just type the caption text and send it.\n\n"
    "To create a poll:\n"
    "- Start your message with /poll followed by the question and each option separated by a vertical bar |.\n"
    "- Format: /poll Question|Option 1|Option 2|Option 3\n"
    "- Example: /poll Which color do you prefer?|Red|Blue|Green\n\n"
    "Important:\n"
    "- Polls must have at least 2 options (and up to 10).\n"
    "- The first item after /poll is the poll question; the remaining items are the options.\n"
    "- Avoid using | inside option text (use it only as the separator).\n\n"
    "After you send the caption or /poll message the bot will preview the photo with your caption or poll.\n"
    "Tap Confirm to forward it for approval, or New Input to change it."
)

def validate_config():
    missing = []
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == 'YOUR_BOT_TOKEN_HERE':
        missing.append('TELEGRAM_BOT_TOKEN')
    if not CHANNEL_ID or CHANNEL_ID == 'your_channel_id_here':
        missing.append('CHANNEL_ID')
    if not OWNER_CHAT_ID or OWNER_CHAT_ID == 'owner_chat_id':
        missing.append('OWNER_CHAT_ID')
    if missing:
        print('Missing or placeholder configuration for:', ', '.join(missing))
        print('Please update the .env file or set environment variables. See README.md for details.')
        sys.exit(1)


def safe_edit_or_reply(query, text: str):
    """Try to edit the callback message text or caption; fall back to replying if edit fails."""
    try:
        # If message contains photo/media, edit caption instead of text
        if query.message and getattr(query.message, 'photo', None):
            logger.info('safe_edit_or_reply: editing caption for photo message')
            try:
                query.edit_message_caption(caption=text)
                return
            except Exception as e:
                logger.warning('edit_message_caption failed: %s', e)
        # Otherwise, try editing text
        try:
            query.edit_message_text(text=text)
            return
        except Exception as e:
            logger.warning('edit_message_text failed: %s', e)
        # Fallback: send a new message to the user
        try:
            query.message.reply_text(text)
        except Exception as e:
            logger.error('fallback reply_text also failed: %s', e)
    except Exception as e:
        logger.exception('safe_edit_or_reply unexpected error: %s', e)

# Define a command handler. This usually takes the two arguments update and context.
def start(update: Update, context: CallbackContext) -> None:
    logger.info('HANDLER start: user=%s', update.effective_user and update.effective_user.id)
    update.message.reply_text('Hi! Send me an image and I will forward it to the channel.')

def handle_image(update: Update, context: CallbackContext) -> None:
    logger.info('HANDLER handle_image: called by user=%s', update.effective_user and update.effective_user.id)
    # Check if the message contains a photo
    if update.message and update.message.photo:
        # Get the file ID of the photo
        file_id = update.message.photo[-1].file_id
        logger.info('handle_image: got file_id=%s', file_id)
        # Store the file ID in user data
        context.user_data['image_file_id'] = file_id
        logger.info('handle_image: stored image_file_id in user_data')
        # Ask the user if they want to add a caption or poll
        keyboard = [[InlineKeyboardButton("Yes", callback_data='add_caption_poll')],
                    [InlineKeyboardButton("No", callback_data='no_caption_poll')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        update.message.reply_text('Image received. Do you need to add a caption or poll to it?', reply_markup=reply_markup)
        logger.info('handle_image: asked user to add caption/poll')
    else:
        logger.info('handle_image: no photo in message')
        update.message.reply_text('Please send a photo.')

def button(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    query.answer()
    data = query.data
    user_id = update.effective_user and update.effective_user.id
    logger.info('HANDLER button: callback from user=%s data=%s', user_id, data)

    if data == 'add_caption_poll':
        logger.info('button: user chose to add caption/poll')
        safe_edit_or_reply(query, GUIDE_TEXT)
        return

    if data == 'no_caption_poll':
        logger.info('button: user chose no caption/poll; creating approval and forwarding to owner')
        # Create approval entry from current user's context
        file_id = context.user_data.get('image_file_id')
        if not file_id:
            logger.warning('no image_file_id in user_data when creating approval')
            safe_edit_or_reply(query, 'No image found to forward for approval.')
            return
        approval_id = str(uuid4())
        APPROVALS[approval_id] = {'file_id': file_id, 'caption': None, 'poll': None}
        # send to owner
        keyboard = [[InlineKeyboardButton("Approve", callback_data=f'approve:{approval_id}'),
                     InlineKeyboardButton("Disapprove", callback_data=f'disapprove:{approval_id}')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        context.bot.send_photo(chat_id=OWNER_CHAT_ID, photo=file_id, caption='Approval request', reply_markup=reply_markup)
        safe_edit_or_reply(query, "Image forwarded to the channel for approval.")
        return

    # Confirmation and approval flows
    if data == 'confirm_caption':
        logger.info('button: received confirm_caption')
        if 'image_file_id' in context.user_data and 'caption' in context.user_data:
            file_id = context.user_data['image_file_id']
            caption = context.user_data['caption']
            logger.info('button: confirm_caption creating approval entry and forwarding to owner')
            approval_id = str(uuid4())
            APPROVALS[approval_id] = {'file_id': file_id, 'caption': caption, 'poll': None}
            keyboard = [[InlineKeyboardButton("Approve", callback_data=f'approve:{approval_id}'),
                         InlineKeyboardButton("Disapprove", callback_data=f'disapprove:{approval_id}')]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            context.bot.send_photo(chat_id=OWNER_CHAT_ID, photo=file_id, caption=caption, reply_markup=reply_markup)
            safe_edit_or_reply(query, "Image with caption forwarded to the channel for approval.")
        return

    if data == 'confirm_poll':
        logger.info('button: received confirm_poll')
        if 'image_file_id' in context.user_data and 'poll_options' in context.user_data:
            file_id = context.user_data['image_file_id']
            poll_question = context.user_data.get('poll_question', 'Poll Question')
            poll_options = context.user_data['poll_options']
            logger.info('button: confirm_poll creating approval entry and forwarding to owner')
            approval_id = str(uuid4())
            APPROVALS[approval_id] = {'file_id': file_id, 'caption': poll_question, 'poll': poll_options}
            keyboard = [[InlineKeyboardButton("Approve", callback_data=f'approve:{approval_id}'),
                         InlineKeyboardButton("Disapprove", callback_data=f'disapprove:{approval_id}')]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            context.bot.send_photo(chat_id=OWNER_CHAT_ID, photo=file_id, caption=poll_question, reply_markup=reply_markup)
            safe_edit_or_reply(query, "Image with poll forwarded to the channel for approval.")
        return

    if data == 'new_input':
        logger.info('button: received new_input')
        safe_edit_or_reply(query, GUIDE_TEXT)
        return

    if data == 'approve':
        logger.info('button: received approve without id — ignoring')
        return

    if data == 'disapprove':
        logger.info('button: received disapprove without id — ignoring')
        return

    # Handle owner approve/disapprove with approval_id encoded
    if data and data.startswith('approve:'):
        approval_id = data.split(':', 1)[1]
        logger.info('button: owner approve for id=%s', approval_id)
        approval = APPROVALS.pop(approval_id, None)
        if not approval:
            safe_edit_or_reply(query, 'Approval item not found or already processed.')
            return
        file_id = approval.get('file_id')
        caption = approval.get('caption')
        poll = approval.get('poll')
        forward_to_channel(context, file_id, caption, caption if poll else None, poll)
        safe_edit_or_reply(query, 'Message approved and forwarded to the channel.')
        return

    if data and data.startswith('disapprove:'):
        approval_id = data.split(':', 1)[1]
        logger.info('button: owner disapprove for id=%s', approval_id)
        approval = APPROVALS.pop(approval_id, None)
        safe_edit_or_reply(query, 'Message disapproved.')
        return
        return

def handle_caption_poll(update: Update, context: CallbackContext) -> None:
    logger.info('HANDLER handle_caption_poll: called by user=%s', update.effective_user and update.effective_user.id)
    user_input = update.message.text
    logger.info('handle_caption_poll: user_input=%s', user_input)
    if 'image_file_id' in context.user_data:
        file_id = context.user_data['image_file_id']
        # Check if the input is a poll or a caption
        if user_input.startswith('/poll '):
            poll_options = user_input[len('/poll '):].split('|')
            if len(poll_options) < 2:
                update.message.reply_text('Please provide at least two options for the poll, separated by |.')
                return
            poll_question = poll_options[0]
            poll_options = poll_options[1:]
            keyboard = [[InlineKeyboardButton("Confirm", callback_data='confirm_poll'),
                         InlineKeyboardButton("New Input", callback_data='new_input')]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            logger.info('handle_caption_poll: sending photo preview with poll question')
            context.bot.send_photo(chat_id=update.effective_chat.id, photo=file_id, caption=poll_question, reply_markup=reply_markup)
            context.user_data['poll_options'] = poll_options
            context.user_data['poll_question'] = poll_question
        else:
            keyboard = [[InlineKeyboardButton("Confirm", callback_data='confirm_caption'),
                         InlineKeyboardButton("New Input", callback_data='new_input')]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            logger.info('handle_caption_poll: sending photo preview with caption')
            context.bot.send_photo(chat_id=update.effective_chat.id, photo=file_id, caption=user_input, reply_markup=reply_markup)
            context.user_data['caption'] = user_input
    else:
        update.message.reply_text('No image found. Please send an image first.')


def forward_to_owner(update: Update, context: CallbackContext, caption: str = None, poll: list = None) -> None:
    file_id = context.user_data['image_file_id']
    logger.info('forward_to_owner: sending to owner=%s file_id=%s caption=%s poll=%s', OWNER_CHAT_ID, file_id, bool(caption), bool(poll))
    if caption:
        keyboard = [[InlineKeyboardButton("Approve", callback_data='approve'),
                     InlineKeyboardButton("Disapprove", callback_data='disapprove')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        context.bot.send_photo(chat_id=OWNER_CHAT_ID, photo=file_id, caption=caption, reply_markup=reply_markup)
    elif poll:
        poll_question = poll[0]
        poll_options = poll[1:]
        keyboard = [[InlineKeyboardButton("Approve", callback_data='approve'),
                     InlineKeyboardButton("Disapprove", callback_data='disapprove')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        context.bot.send_poll(chat_id=OWNER_CHAT_ID, question=poll_question, options=poll_options, photo=file_id, reply_markup=reply_markup)

def forward_to_channel(context: CallbackContext, file_id: str, caption: str = None, poll_question: str = None, poll_options: list = None) -> None:
    logger.info('forward_to_channel: sending to channel=%s file_id=%s caption=%s poll_question=%s', CHANNEL_ID, file_id, bool(caption), poll_question)
    if caption:
        context.bot.send_photo(chat_id=CHANNEL_ID, photo=file_id, caption=caption)
    elif poll_question and poll_options:
        context.bot.send_poll(chat_id=CHANNEL_ID, question=poll_question, options=poll_options, photo=file_id)

def main() -> None:
    # Validate config and create the Updater using the token from the environment
    validate_config()
    # Create the Updater and pass it your bot's token.
    updater = Updater(TELEGRAM_BOT_TOKEN)

    # Get the dispatcher to register handlers
    dispatcher = updater.dispatcher

    # on different commands - answer in Telegram
    dispatcher.add_handler(CommandHandler("start", start))

    # on non-command i.e message - echo the message on Telegram
    dispatcher.add_handler(MessageHandler(Filters.photo, handle_image))
    dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_caption_poll))
    dispatcher.add_handler(CallbackQueryHandler(button))

    # Start the Bot
    updater.start_polling()

    # Run the bot until you press Ctrl-C or the process receives SIGINT, SIGTERM or SIGABRT
    updater.idle()

if __name__ == '__main__':
    main()