import base64
import subprocess
import math
from io import BytesIO

from PIL import ImageFont
from PyPDF2 import PdfFileWriter, PdfFileReader
from flask import request, abort, send_file, Blueprint, jsonify, current_app
from notifications_utils.statsd_decorators import statsd
from notifications_utils.formatters import unescaped_formatted_list
from pdf2image import convert_from_bytes
from reportlab.lib.colors import white, black, Color
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from app import auth, InvalidRequest
from app.preview import png_from_pdf, pngs_from_pdf
from app.transformation import convert_pdf_to_cmyk, does_pdf_contain_cmyk, does_pdf_contain_rgb

NOTIFY_TAG_FROM_TOP_OF_PAGE = 4.3
NOTIFY_TAG_FROM_LEFT_OF_PAGE = 7.4
NOTIFY_TAG_FONT_SIZE = 6
NOTIFY_TAG_TEXT = "NOTIFY"
NOTIFY_TAG_LINE_SPACING = 1.75
ADDRESS_FONT_SIZE = 8
ADDRESS_LINE_HEIGHT = ADDRESS_FONT_SIZE + 0.5
FONT = "Arial"
TRUE_TYPE_FONT_FILE = FONT + ".ttf"

BORDER_FROM_BOTTOM_OF_PAGE = 5.0
BORDER_FROM_TOP_OF_PAGE = 5.0
BORDER_FROM_LEFT_OF_PAGE = 15.0
BORDER_FROM_RIGHT_OF_PAGE = 15.0
BODY_TOP_FROM_TOP_OF_PAGE = 95.00

SERVICE_ADDRESS_LEFT_FROM_LEFT_OF_PAGE = 125.0
SERVICE_ADDRESS_BOTTOM_FROM_TOP_OF_PAGE = 95.00

ADDRESS_TOP_FROM_TOP_OF_PAGE = 39.50
ADDRESS_LEFT_FROM_LEFT_OF_PAGE = 24.60
ADDRESS_BOTTOM_FROM_TOP_OF_PAGE = 66.30
ADDRESS_RIGHT_FROM_LEFT_OF_PAGE = 120.0

ADDRESS_HEIGHT = ADDRESS_BOTTOM_FROM_TOP_OF_PAGE - ADDRESS_TOP_FROM_TOP_OF_PAGE
ADDRESS_WIDTH = ADDRESS_RIGHT_FROM_LEFT_OF_PAGE - ADDRESS_LEFT_FROM_LEFT_OF_PAGE

LOGO_LEFT_FROM_LEFT_OF_PAGE = 15.00
LOGO_RIGHT_FROM_LEFT_OF_PAGE = SERVICE_ADDRESS_LEFT_FROM_LEFT_OF_PAGE
LOGO_BOTTOM_FROM_TOP_OF_PAGE = 30.00
LOGO_TOP_FROM_TOP_OF_PAGE = 5.00

LOGO_HEIGHT = LOGO_BOTTOM_FROM_TOP_OF_PAGE - LOGO_TOP_FROM_TOP_OF_PAGE
LOGO_WIDTH = LOGO_RIGHT_FROM_LEFT_OF_PAGE - LOGO_LEFT_FROM_LEFT_OF_PAGE

A4_WIDTH = 210 * mm
A4_HEIGHT = 297 * mm

precompiled_blueprint = Blueprint('precompiled_blueprint', __name__)


@precompiled_blueprint.route('/precompiled/sanitise', methods=['POST'])
@auth.login_required
@statsd(namespace='template_preview')
def sanitise_precompiled_letter():
    """
    Given a PDF, returns a new PDF that has been sanitised and dvla approved 👍

    * makes sure letter is within dvla's printable boundaries
    * re-writes address block (to ensure it's in arial in the right location)
    * adds NOTIFY tag (regardless of whether it's there or not)
    """
    encoded_string = request.get_data()

    if not encoded_string:
        raise InvalidRequest('Sanitise failed - No encoded string')

    file_data = BytesIO(encoded_string)
    invalid_pages, message = get_invalid_pages_with_message(file_data)
    if len(invalid_pages) > 0:
        raise InvalidRequest(message)

    # during switchover, DWP will still be sending the notify tag. Only add it if it's not already there
    if not does_pdf_contain_cmyk(encoded_string) or does_pdf_contain_rgb(encoded_string):
        file_data = BytesIO(convert_pdf_to_cmyk(encoded_string))
    if not is_notify_tag_present(file_data):
        file_data = add_notify_tag_to_letter(file_data)

    # TODO Address validation is disabled until we have more confidence it does the right thing
    # file_data = rewrite_address_block(file_data)

    return send_file(filename_or_fp=file_data, mimetype='application/pdf')


@precompiled_blueprint.route("/precompiled/add_tag", methods=['POST'])
@auth.login_required
@statsd(namespace="template_preview")
def add_tag_to_precompiled_letter():
    encoded_string = request.get_data()

    if not encoded_string:
        abort(400)

    file_data = BytesIO(encoded_string)

    return send_file(filename_or_fp=add_notify_tag_to_letter(file_data), mimetype='application/pdf')


@precompiled_blueprint.route("/precompiled/validate", methods=['POST'])
@auth.login_required
@statsd(namespace="template_preview")
def validate_pdf_document():
    encoded_string = request.get_data()
    generate_preview_pngs = request.args.get('include_preview') in ['true', 'True', '1']

    if not encoded_string:
        abort(400)

    invalid_pages, message = get_invalid_pages_with_message(BytesIO(encoded_string))
    data = {
        'result': len(invalid_pages) == 0,
    }

    if not generate_preview_pngs:
        return jsonify(data)

    if invalid_pages:
        data['message'] = message
        pages = overlay_template_areas(BytesIO(encoded_string), overlay=True)

    else:
        data['message'] = 'Your PDF passed the layout check'
        file_data = rewrite_address_block(BytesIO(encoded_string))
        pages = pngs_from_pdf(file_data)

    data['pages'] = [
        base64.b64encode(page.read()).decode('ascii') for page in pages
    ]

    return jsonify(data)


@precompiled_blueprint.route("/precompiled/overlay.<file_type>", methods=['POST'])
@auth.login_required
@statsd(namespace="template_preview")
def overlay_template(file_type):
    encoded_string = request.get_data()

    if not encoded_string:
        abort(400)

    file_data = BytesIO(encoded_string)

    if file_type == 'png':
        return send_file(
            filename_or_fp=png_from_pdf(
                _colour_no_print_areas(file_data, page_number=int(request.args.get('page_number', 1))),
                int(request.args.get('page', 1))
            ),
            mimetype='image/png',
        )
    else:
        return send_file(
            filename_or_fp=_colour_no_print_areas(
                file_data,
            ),
            mimetype='application/pdf',
        )


def add_notify_tag_to_letter(src_pdf):
    """
    Adds the word 'NOTIFY' to the first page of the PDF

    :param PdfFileReader src_pdf: A File object or an object that supports the standard read and seek methods
    """

    pdf = PdfFileReader(src_pdf)
    output = PdfFileWriter()
    page = pdf.getPage(0)
    packet = BytesIO()
    can = canvas.Canvas(packet, pagesize=A4)
    pdfmetrics.registerFont(TTFont(FONT, TRUE_TYPE_FONT_FILE))
    can.setFillColorRGB(255, 255, 255)  # white
    can.setFont(FONT, NOTIFY_TAG_FONT_SIZE)

    font = ImageFont.truetype(TRUE_TYPE_FONT_FILE, NOTIFY_TAG_FONT_SIZE)
    line_width, line_height = font.getsize('NOTIFY')

    center_of_left_margin = (BORDER_FROM_LEFT_OF_PAGE * mm) / 2
    half_width_of_notify_tag = line_width / 2
    x = center_of_left_margin - half_width_of_notify_tag

    # page.mediaBox[3] Media box is an array with the four corners of the page
    # We want height so can use that co-ordinate which is located in [3]
    # The lets take away the margin and the ont size
    # 1.75 for the line spacing
    y = float(page.mediaBox[3]) - (float(NOTIFY_TAG_FROM_TOP_OF_PAGE * mm + line_height - NOTIFY_TAG_LINE_SPACING))

    can.drawString(x, y, NOTIFY_TAG_TEXT)
    can.save()

    # move to the beginning of the StringIO buffer
    packet.seek(0)
    new_pdf = PdfFileReader(packet)

    new_page = new_pdf.getPage(0)
    new_page.mergePage(page)
    output.addPage(new_page)

    # add the rest of the document to the new doc. NOTIFY only appears on the first page
    for page_num in range(1, pdf.numPages):
        output.addPage(pdf.getPage(page_num))

    pdf_bytes = BytesIO()
    output.write(pdf_bytes)
    pdf_bytes.seek(0)

    return pdf_bytes


def overlay_template_areas(src_pdf, page_number=None, overlay=True):
    pdf = _overlay_printable_areas(src_pdf, overlay=overlay)
    if page_number is None:
        return pngs_from_pdf(pdf)
    return png_from_pdf(pdf, page_number)


def get_invalid_pages_with_message(src_pdf):
    message = ""
    invalid_pages = []
    invalid_pages = _get_pages_with_invalid_orientation_or_size(src_pdf)
    if len(invalid_pages) > 0:
        message = "Your letter is not A4 portrait size on "
    else:
        pdf_to_validate = _overlay_printable_areas(src_pdf)
        invalid_pages = list(_get_out_of_bounds_pages(PdfFileReader(pdf_to_validate)))
        if len(invalid_pages) > 0:
            message = 'Content in this PDF is outside the printable area on '
    if len(invalid_pages) > 0:
        message += unescaped_formatted_list(
            invalid_pages,
            before_each='',
            after_each='',
            prefix='page',
            prefix_plural='pages'
        )
    return (invalid_pages, message)


def _is_page_A4_portrait(page_height, page_width, rotation):
    if math.isclose(page_height, 297, abs_tol=2) and math.isclose(page_width, 210, abs_tol=2):
        if rotation in [0, 180, None]:
            return True
    elif math.isclose(page_width, 297, abs_tol=2) and math.isclose(page_height, 210, abs_tol=2):
        if rotation in [90, 270]:
            return True
    return False


def _get_pages_with_invalid_orientation_or_size(src_pdf):
    pdf = PdfFileReader(src_pdf)
    invalid_pages = []
    for page_num in range(0, pdf.numPages):
        page = pdf.getPage(page_num)

        page_height = float(page.mediaBox.getHeight()) / mm
        page_width = float(page.mediaBox.getWidth()) / mm
        rotation = page.get('/Rotate')

        if not _is_page_A4_portrait(page_height, page_width, rotation):
            invalid_pages.append(page_num + 1)
            current_app.logger.warning(
                "Letter is not A4 portrait size on page {}. Rotate: {}, height: {}mm, width: {}mm".format(
                    page_num + 1, rotation, int(page_height), int(page_width)
                )
            )

        return invalid_pages


def _overlay_printable_areas(src_pdf, overlay=False):
    """
    Overlays the printable areas onto the src PDF, this is so the code can check for a presence of non white in the
    areas outside the printable area.

    :param BytesIO src_pdf: A file-like
    :param bool overlay: overlay the template as a red opaque block otherwise just block white
    """

    pdf = PdfFileReader(src_pdf)
    output = PdfFileWriter()
    page = pdf.getPage(0)
    packet = BytesIO()
    can = canvas.Canvas(packet, pagesize=A4)

    page_height = float(page.mediaBox.getHeight())
    page_width = float(page.mediaBox.getWidth())

    red_transparent = Color(100, 0, 0, alpha=0.2)

    colour = red_transparent if overlay else white

    can.setStrokeColor(colour)
    can.setFillColor(colour)

    width = page_width - (BORDER_FROM_LEFT_OF_PAGE * mm + BORDER_FROM_RIGHT_OF_PAGE * mm)

    # Overlay the blanks where the service can print as per the template
    # The first page is more varied because of address blocks etc subsequent pages are more simple

    # Body
    x = BORDER_FROM_LEFT_OF_PAGE * mm
    y = BORDER_FROM_BOTTOM_OF_PAGE * mm

    height = page_height - ((BODY_TOP_FROM_TOP_OF_PAGE + BORDER_FROM_BOTTOM_OF_PAGE) * mm)
    can.rect(x, y, width, height, fill=True, stroke=False)

    # Service address block
    x = SERVICE_ADDRESS_LEFT_FROM_LEFT_OF_PAGE * mm
    y = page_height - (SERVICE_ADDRESS_BOTTOM_FROM_TOP_OF_PAGE * mm)

    service_address_width = page_width - (SERVICE_ADDRESS_LEFT_FROM_LEFT_OF_PAGE * mm + BORDER_FROM_RIGHT_OF_PAGE * mm)

    height = (SERVICE_ADDRESS_BOTTOM_FROM_TOP_OF_PAGE - BORDER_FROM_TOP_OF_PAGE) * mm
    can.rect(x, y, service_address_width, height, fill=True, stroke=False)

    # Service Logo Block
    x = LOGO_LEFT_FROM_LEFT_OF_PAGE * mm
    y = page_height - (LOGO_BOTTOM_FROM_TOP_OF_PAGE * mm)

    can.rect(x, y, LOGO_WIDTH * mm, LOGO_HEIGHT * mm, fill=True, stroke=False)

    # Citizen Address Block
    x = ADDRESS_LEFT_FROM_LEFT_OF_PAGE * mm
    y = page_height - (ADDRESS_BOTTOM_FROM_TOP_OF_PAGE * mm)

    address_block_width = ADDRESS_WIDTH * mm

    height = (ADDRESS_BOTTOM_FROM_TOP_OF_PAGE - ADDRESS_TOP_FROM_TOP_OF_PAGE) * mm
    can.rect(x, y, address_block_width, height, fill=True, stroke=False)

    can.save()

    # move to the beginning of the StringIO buffer
    packet.seek(0)
    new_pdf = PdfFileReader(packet)

    page.mergePage(new_pdf.getPage(0))
    output.addPage(page)

    # For each subsequent page its just the body of text
    for page_num in range(1, pdf.numPages):
        page = pdf.getPage(page_num)

        page_height = float(page.mediaBox.getHeight())
        page_width = float(page.mediaBox.getWidth())

        packet = BytesIO()
        can = canvas.Canvas(packet, pagesize=A4)

        can.setStrokeColor(colour)
        can.setFillColor(colour)

        # Each page of content
        x = BORDER_FROM_LEFT_OF_PAGE * mm
        y = BORDER_FROM_BOTTOM_OF_PAGE * mm
        height = page_height - ((BORDER_FROM_TOP_OF_PAGE + BORDER_FROM_BOTTOM_OF_PAGE) * mm)
        width = page_width - (BORDER_FROM_LEFT_OF_PAGE * mm + BORDER_FROM_RIGHT_OF_PAGE * mm)
        can.rect(x, y, width, height, fill=True, stroke=False)
        can.save()

        # move to the beginning of the StringIO buffer
        packet.seek(0)
        new_pdf = PdfFileReader(packet)

        page.mergePage(new_pdf.getPage(0))
        output.addPage(page)

    pdf_bytes = BytesIO()
    output.write(pdf_bytes)
    pdf_bytes.seek(0)

    # it's a good habit to put things back exactly the way we found them
    src_pdf.seek(0)

    return pdf_bytes


def _colour_no_print_areas(src_pdf, page_number=1):
    """
    Overlays the non-printable areas onto the src PDF, this is so users know which parts of they letter fail validation.

    :param BytesIO src_pdf: A file-like
    """
    pdf = PdfFileReader(src_pdf)
    output = PdfFileWriter()

    page = pdf.getPage(0)
    packet = BytesIO()
    can = canvas.Canvas(packet, pagesize=A4)

    page_height = float(page.mediaBox.getHeight())
    page_width = float(page.mediaBox.getWidth())

    colour = Color(100, 0, 0, alpha=0.2)  # red transparent
    can.setStrokeColor(colour)
    can.setFillColor(colour)

    # Overlay the areas where the service can't print as per the template
    # The first page is more varied because of address blocks etc subsequent pages are more simple

    # Margins
    left = BORDER_FROM_LEFT_OF_PAGE * mm
    bottom = BORDER_FROM_BOTTOM_OF_PAGE * mm
    right = BORDER_FROM_RIGHT_OF_PAGE * mm
    top = BORDER_FROM_TOP_OF_PAGE * mm
    # left margin:
    can.rect(0, 0, left, page_height, fill=True, stroke=False)
    # top margin:
    can.rect(left, page_height - top, page_width - (2 * right), page_height, fill=True, stroke=False)
    # right margin:
    can.rect(page_width - right, 0, page_width, page_height, fill=True, stroke=False)
    # bottom margin:
    can.rect(left, 0, page_width - (2 * right), bottom, fill=True, stroke=False)

    if page_number == 1:
        # Body
        body_top = BODY_TOP_FROM_TOP_OF_PAGE * mm
        # Service address
        service_left = SERVICE_ADDRESS_LEFT_FROM_LEFT_OF_PAGE * mm
        # Citizen's address
        address_bottom = ADDRESS_BOTTOM_FROM_TOP_OF_PAGE * mm
        address_top = ADDRESS_TOP_FROM_TOP_OF_PAGE * mm
        address_left = ADDRESS_LEFT_FROM_LEFT_OF_PAGE * mm
        address_right = ADDRESS_RIGHT_FROM_LEFT_OF_PAGE * mm
        # Logo
        logo_bottom = LOGO_BOTTOM_FROM_TOP_OF_PAGE * mm

        # left from address block
        can.rect(
            left, page_height - address_bottom, address_left - left,
            address_bottom - address_top, fill=True, stroke=False
        )
        # above address block
        can.rect(
            left, page_height - address_top, service_left - left, address_top - logo_bottom, fill=True, stroke=False
        )
        # right from address block
        can.rect(
            address_right, page_height - address_bottom, service_left - address_right, address_bottom - address_top,
            fill=True, stroke=False
        )
        # below address block
        can.rect(left, page_height - body_top, service_left - left, body_top - address_bottom, fill=True, stroke=False)
    can.save()

    # move to the beginning of the StringIO buffer
    packet.seek(0)
    new_pdf = PdfFileReader(packet)

    page.mergePage(new_pdf.getPage(0))
    output.addPage(page)
    # For each subsequent page its just the body of text
    for page_num in range(1, pdf.numPages):
        page = pdf.getPage(page_num)

        page_height = float(page.mediaBox.getHeight())
        page_width = float(page.mediaBox.getWidth())

        packet = BytesIO()
        can = canvas.Canvas(packet, pagesize=A4)

        can.setStrokeColor(colour)
        can.setFillColor(colour)

        # Each page of content
        # left margin:
        can.rect(0, 0, left, page_height, fill=True, stroke=False)
        # top margin:
        can.rect(left, page_height - top, page_width - (2 * right), page_height, fill=True, stroke=False)
        # right margin:
        can.rect(page_width - right, 0, page_width, page_height, fill=True, stroke=False)
        # bottom margin:
        can.rect(left, 0, page_width - (2 * right), bottom, fill=True, stroke=False)
        can.save()

        # move to the beginning of the StringIO buffer
        packet.seek(0)
        new_pdf = PdfFileReader(packet)

        page.mergePage(new_pdf.getPage(0))
        output.addPage(page)

    pdf_bytes = BytesIO()
    output.write(pdf_bytes)
    pdf_bytes.seek(0)

    # it's a good habit to put things back exactly the way we found them
    src_pdf.seek(0)
    return pdf_bytes


def _get_out_of_bounds_pages(src_pdf):
    """
    Checks each pixel of the image to determine the colour - if any pixel is not white return false
    :param PdfFileReader src_pdf: PDF from which to take pages.
    :return: iterable containing page numbers (1-indexed)
    :return: False if there is any colour but white, otherwise true
    """

    dst_pdf = PdfFileWriter()

    pages = src_pdf.numPages

    for page_num in range(0, pages):
        dst_pdf.addPage(src_pdf.getPage(page_num))

    pdf_bytes = BytesIO()
    dst_pdf.write(pdf_bytes)
    pdf_bytes.seek(0)

    images = convert_from_bytes(pdf_bytes.read())

    for i, image in enumerate(images, start=1):
        colours = image.convert('RGB').getcolors()

        if colours is None:
            current_app.logger.error('Letter has literally zero colours of any description on page {}???'.format(i))
            yield i
            continue

        for colour in colours:
            if str(colour[1]) != "(255, 255, 255)":
                current_app.logger.warning('Letter exceeds boundaries on page {}'.format(i))
                yield i
                break


def rewrite_address_block(pdf):
    address = extract_address_block(pdf)

    pdf = add_address_to_precompiled_letter(pdf, address)

    return pdf


def _extract_text_from_pdf(pdf, *, x, y, width, height):
    """
    Extracts all text within a block.

    pdf is a BytesIO or other file-like.
    x, y are coordinates in mm from the top left of the page
    width, height are lengths in mm
    """
    ret = subprocess.run(
        [
            'pdftotext',
            # -layout helps keep things on their correct lines
            '-layout',
            # encode output as utf-8
            '-enc', 'UTF-8',
            # -f and -l: only select page 1
            '-f', '1',
            '-l', '1',
            # x/y coordinates in points (1/72th of an inch)
            '-x', '{}'.format(int(x * mm)),
            '-y', '{}'.format(int(y * mm)),
            # width and height of area in points
            '-W', '{}'.format(int(width * mm)),
            '-H', '{}'.format(int(height * mm)),
            '-',
            '-',
        ],
        input=pdf.read(),
        stdout=subprocess.PIPE
    )
    pdf.seek(0)
    return '\n'.join(
        line.strip()
        for line in ret.stdout.decode('utf-8').split('\n')
        if line.strip()
    )


def extract_address_block(pdf):
    """
    Extracts all text within the text block

    :param BytesIO pdf: pdf bytestream from which to extract
    :return: multi-line address string
    """

    # add on a margin to ensure we capture all text
    x = ADDRESS_LEFT_FROM_LEFT_OF_PAGE - 3
    y = ADDRESS_TOP_FROM_TOP_OF_PAGE - 3
    width = ADDRESS_WIDTH + 6
    height = ADDRESS_HEIGHT + 6
    return _extract_text_from_pdf(
        pdf,
        x=x,
        y=y,
        width=width,
        height=height,
    )


def is_notify_tag_present(pdf):
    """
    pdf is a file-like object containing at least the first page of a PDF
    """
    font = ImageFont.truetype(TRUE_TYPE_FONT_FILE, NOTIFY_TAG_FONT_SIZE)
    line_width, line_height = font.getsize('NOTIFY')

    # add on a fairly chunky margin to be generous to rounding errors
    x = NOTIFY_TAG_FROM_LEFT_OF_PAGE - 5
    y = NOTIFY_TAG_FROM_TOP_OF_PAGE - 3
    # font.getsize returns values in points, we need to get back into mm
    width = (line_width / mm) + 10
    height = (line_height / mm) + 6

    return _extract_text_from_pdf(
        pdf,
        x=x,
        y=y,
        width=width,
        height=height
    ) == 'NOTIFY'


def add_address_to_precompiled_letter(pdf, address):
    """
    Given a pdf, blanks out any existing address (adds a white rectangle over existing address),
    and then puts the supplied address in over it.

    :param BytestIO pdf: pdf bytestream from which to extract
    :return: BytesIO new pdf
    """
    new_page_buffer = BytesIO()
    old_pdf = PdfFileReader(pdf)

    can = canvas.Canvas(new_page_buffer, pagesize=A4)

    # x, y coordinates are from bottom left of page
    bottom_left_corner_x = ADDRESS_LEFT_FROM_LEFT_OF_PAGE * mm
    bottom_left_corner_y = A4_HEIGHT - (ADDRESS_BOTTOM_FROM_TOP_OF_PAGE * mm)

    # Cover the existing address block with a white rectangle
    can.setFillColor(white)
    can.rect(
        x=bottom_left_corner_x,
        y=bottom_left_corner_y,
        width=ADDRESS_WIDTH * mm,
        height=ADDRESS_HEIGHT * mm,
        fill=True,
        stroke=False
    )

    # start preparing to write address
    pdfmetrics.registerFont(TTFont(FONT, TRUE_TYPE_FONT_FILE))

    # text origin is bottom left of the first character. But we've got multiple lines, and we want to match the
    # bottom left of the bottom line of text to the bottom left of the address block.
    # So calculate the number of additional lines by counting the newlines, and multiply that by the line height
    address_lines_after_first = address.count('\n')
    first_character_of_address = bottom_left_corner_y + (ADDRESS_LINE_HEIGHT * address_lines_after_first)

    textobject = can.beginText()
    textobject.setFillColor(black)
    textobject.setFont(FONT, ADDRESS_FONT_SIZE, leading=ADDRESS_LINE_HEIGHT)
    # push the text up two points (25%) in case the last line (postcode) has any chars with descenders - g, j, p, q, y.
    # we don't want them going out of the address window
    textobject.setRise(2)
    textobject.setTextOrigin(bottom_left_corner_x, first_character_of_address)
    textobject.textLines(address)
    can.drawText(textobject)

    can.save()

    return replace_first_page_of_pdf(old_pdf, new_page_buffer)


def replace_first_page_of_pdf(old_pdf, new_page_buffer):
    # move to the beginning of the buffer and replay it into a pdf writer
    new_page_buffer.seek(0)
    new_pdf = PdfFileReader(new_page_buffer)
    output = PdfFileWriter()
    new_page = new_pdf.getPage(0)
    existing_page = old_pdf.getPage(0)

    existing_page.mergePage(new_page)
    output.addPage(existing_page)

    # add the rest of the document to the new doc, we only change the address on the first page
    for page_num in range(1, old_pdf.numPages):
        output.addPage(old_pdf.getPage(page_num))

    new_pdf_buffer = BytesIO()
    output.write(new_pdf_buffer)
    new_pdf_buffer.seek(0)

    return new_pdf_buffer
