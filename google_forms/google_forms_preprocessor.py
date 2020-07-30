from boltons import iterutils
from googleapiclient import http
from grow.common.utils import bs4
from grow.preprocessors import google_drive
from protorpc import messages
from protorpc import protojson
import grow
import io
import json
import os
import re
import requests


TRANSLATABLE_KEYS = (
    'description',
    'label',
    'placeholder',
    'title',
    'value',
)

SIGN_IN_PAGE_SENTINEL = \
    '<title>Google Forms - create and analyze surveys, for free.</title>'


def nl2br(value):
    _paragraph_re = re.compile(r'(?:\r\n|\r(?!\n)|\n){2,}')
    return u'\n\n'.join(u'<p>%s</p>' % p.replace(u'\n', '<br>\n')
                        for p in _paragraph_re.split(value))


class Error(Exception):
    pass


class FieldType(messages.Enum):
    TEXT = 1
    TEXTAREA = 2
    CHECKBOX = 3
    RADIO = 4
    SCALE = 5
    DATE = 6


class Field(messages.Message):
    placeholder = messages.StringField(3)
    field_type = messages.EnumField(FieldType, 4)
    name = messages.StringField(5)
    value = messages.StringField(6)


class Header(messages.Message):
    title = messages.StringField(1)
    body = messages.StringField(2)


class GridRow(messages.Message):
    fields = messages.MessageField(Field, 1, repeated=True)
    label = messages.StringField(2)


class Item(messages.Message):
    label = messages.StringField(1)
    description = messages.StringField(2)
    fields = messages.MessageField(Field, 3, repeated=True)
    grid = messages.MessageField(GridRow, 6, repeated=True)
    required = messages.BooleanField(4)
    header = messages.MessageField(Header, 5)


class Form(messages.Message):
    title = messages.StringField(1)
    description = messages.StringField(2)
    items = messages.MessageField(Item, 3, repeated=True)
    action = messages.StringField(4)


class GoogleFormsPreprocessor(google_drive.BaseGooglePreprocessor):
    KIND = 'google_forms'
    VIEW_URL = 'https://docs.google.com/forms/d/e/{}/viewform?hl=en'
    ACTION_URL = 'https://docs.google.com/forms/d/e/{}/formResponse?hl=en'

    class Config(messages.Message):
        id = messages.StringField(1)
        path = messages.StringField(2)
        translate = messages.BooleanField(3, default=True)

    def run(self, *args, **kwargs):
        url = GoogleFormsPreprocessor.VIEW_URL.format(self.config.id)
        resp = requests.get(url)
        if resp.status_code != 200:
            raise Error('Error requesting -> {}'.format(url))
        html = resp.text
        if SIGN_IN_PAGE_SENTINEL in html:
            raise Error(
                'Error requesting -> {} -> Are you sure the form is publicly'
                ' viewable?'.format(url))
        soup = bs4.BeautifulSoup(html, 'html.parser')
        soup_content = soup.find('div', {'class': 'freebirdFormviewerViewFormContent'})
        form_msg = self.parse_form(soup_content)
        msg_string_content = protojson.encode_message(form_msg)
        json_dict = json.loads(msg_string_content)
        if self.config.translate:
            json_dict = self.tag_keys_for_translation(json_dict)
        self.pod.write_yaml(self.config.path, json_dict)
        self.pod.logger.info('Saved -> {}'.format(self.config.path))

    def tag_keys_for_translation(self, data):
        def visit(path, key, value):
            if not isinstance(key, basestring) or not value:
                return key, value
            key = '{}@'.format(key) if key in TRANSLATABLE_KEYS else key
            return key, value
        return iterutils.remap(data, visit=visit)

    def get_html(self, soup, class_name):
        el = soup.find('div', {'class': class_name})
        if not el:
            return
        return ''.join([unicode(part) for part in el.contents])

    def get_text(self, soup, class_name):
        el = soup.find('div', {'class': class_name})
        return el.text if el else None

    def get_placeholder(
            self, soup, class_name='quantumWizTextinputPaperinputPlaceholder'):
        el = soup.find('div', {'class': class_name})
        return el.text if el else None

    def get_description(self, soup):
        el = soup.find('div', {'class': 'freebirdFormviewerViewItemsItemItemHelpText'})
        return el.text if el else None

    def get_choice_value(self, soup):
        el = soup.find('div', {'class': 'docssharedWizToggleLabeledPrimaryText'})
        return el.text if el else None

    def get_header(self, soup):
        header = Header()
        title = soup.find('div', {'class': 'freebirdFormviewerViewItemsSectionheaderTitle'})
        if title:
            header.title = title.text
        body = soup.find('div', {'class': 'freebirdFormviewerViewItemsSectionheaderDescriptionText'})
        if body:
            header.body = body.decode_contents()
        if header.title or header.body:
            return header
        return None

    def parse_form(self, soup):
        msg = Form()
        msg.title = self.get_text(soup, 'freebirdFormviewerViewHeaderTitle')
        msg.description = self.get_html(soup, 'freebirdFormviewerViewHeaderDescription')
        msg.action = GoogleFormsPreprocessor.ACTION_URL.format(self.config.id)
        msg.items = []
        items = soup.findAll('div', {'class': 'freebirdFormviewerViewNumberedItemContainer'})
        for item in items:
            item_msg = Item()
            item_msg.required = bool(item.find('span', {'class': 'freebirdFormviewerComponentsQuestionBaseRequiredAsterisk'}))
            item_msg.label = self.get_text(item, 'exportItemTitle')
            # Strip * from label if required.
            if item_msg.required and item_msg.label.endswith(' *'):
                item_msg.label = item_msg.label[:-2]
            item_msg.description = self.get_description(item)
            item_msg.header = self.get_header(item)
            item_msg.fields = []

            hidden_inputs = item.findAll('input', {'type': 'hidden'})

            params = [div['data-params'] for div in item.find_all() if 'data-params' in div.attrs]
            input_names = []
            if params:
                # First item is not an entry ID, skip it.
                # Skip single digit instances as they're not actually IDs.
                entry_ids = re.findall('\[([0-9]+)', params[0])[1:]
                input_names = ['entry.{}'.format(entry_id) for entry_id in entry_ids if len(entry_id) > 1]

            grid_rows = item.findAll('div', {'class': 'freebirdFormviewerComponentsQuestionGridRowGroup'})
            if grid_rows:
                header = item.find('div', {'class': 'freebirdFormviewerComponentsQuestionGridColumnHeader'})
                input_values = [field.text for field in header.findAll('div', {'class': 'freebirdFormviewerComponentsQuestionGridCell'})]
                item_msg.grid = []
                for i, row in enumerate(grid_rows):
                    label = row.find('div', {'class': 'freebirdFormviewerComponentsQuestionGridRowHeader'})
                    local_hidden_inputs = row.findAll('input', {'type': 'hidden'})
                    choices = row.findAll('div', {'class': 'freebirdFormviewerComponentsQuestionGridCell'})
                    grid_row = GridRow()
                    grid_row.fields = []
                    grid_row.label = label.text
                    for n, choice in enumerate(choices):
                        field_msg = Field()
                        field_msg.field_type = FieldType.RADIO
                        field_msg.name = local_hidden_inputs[0].get('name')
                        field_msg.value = input_values[n]
                        # Skip empty values.
                        if field_msg.value:
                            grid_row.fields.append(field_msg)
                    # Only add rows with labels.
                    if grid_row.label:
                        item_msg.grid.append(grid_row)

            checkboxes = item.findAll('div', {'class': 'freebirdFormviewerComponentsQuestionCheckboxChoice'})
            for checkbox in checkboxes:
                field_msg = Field()
                field_msg.field_type = FieldType.CHECKBOX
                field_msg.name = checkbox.parent.parent.find_all('input')[-1].get('name')
                value = self.get_choice_value(checkbox)
                field_msg.value = value
                item_msg.fields.append(field_msg)
            scales = item.findAll('div', {'class': 'freebirdMaterialScalecontentLabel'})
            for scale in scales:
                field_msg = Field()
                field_msg.field_type = FieldType.SCALE
                field_msg.name = scale.parent.parent.parent.parent.parent.parent.find('input').get('name')
                value = scale.text
                field_msg.value = value
                item_msg.fields.append(field_msg)
            radios = item.findAll('div', {'class': 'freebirdFormviewerComponentsQuestionRadioChoice'})
            for radio in radios:
                field_msg = Field()
                field_msg.field_type = FieldType.RADIO
                # Use findAll[-1] to retrieve the last input, which is the
                # actual one. TODO: Add support for other option response with
                # element name="entry.588393791.other_option_response".
                field_msg.name = radio.parent.parent.parent.parent.parent.parent.findAll('input')[-1].get('name')
                value = self.get_choice_value(radio)
                field_msg.value = value
                item_msg.fields.append(field_msg)
            textareas = item.findAll('div', {'class': 'freebirdFormviewerViewItemsTextLongText'})
            for text in textareas:
                field_msg = Field()
                field_msg.field_type = FieldType.TEXTAREA
                field_msg.placeholder = self.get_placeholder(text, class_name='quantumWizTextinputPapertextareaPlaceholder')
                field_msg.name = input_names[0]
                item_msg.fields.append(field_msg)
            texts = item.findAll('div', {'class': 'freebirdFormviewerComponentsQuestionTextRoot'})
            for text in texts:
                field_msg = Field()
                field_msg.field_type = FieldType.TEXT
                field_msg.placeholder = self.get_placeholder(text)
                field_msg.name = input_names[0]
                item_msg.fields.append(field_msg)
            dates = item.findAll('div', {'class': 'freebirdFormviewerComponentsQuestionDateInputsContainer'})
            for date in dates:
                field_msg = Field()
                field_msg.field_type = FieldType.DATE
                field_msg.placeholder = self.get_placeholder(date)
                field_msg.name = input_names[0]
                item_msg.fields.append(field_msg)
            msg.items.append(item_msg)
        return msg
