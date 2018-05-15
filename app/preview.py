import base64
import dateutil.parser
from io import BytesIO

from flask import Blueprint, request, send_file, abort, current_app, jsonify
from flask_weasyprint import HTML
from notifications_utils.statsd_decorators import statsd
from wand.image import Image
from wand.color import Color
from wand.exceptions import MissingDelegateError
from notifications_utils.template import (
    LetterPreviewTemplate,
    LetterPrintTemplate,
)

from app import auth, cache
from app.schemas import (
    get_and_validate_json_from_request,
    preview_schema,
    get_html_from_request,
)
from app.transformation import convert_pdf_to_cmyk

preview_blueprint = Blueprint('preview_blueprint', __name__)


# When the background is set to white traces of the Notify tag are visible in the preview png
# As modifying the pdf text is complicated, a quick solution is to place a white block over it
def hide_notify_tag(image):
    with Image(width=130, height=50, background=Color('white')) as cover:
        if image.colorspace == 'cmyk':
            cover.transform_colorspace('cmyk')
        image.composite(cover, left=0, top=0)


@statsd(namespace="template_preview")
def png_from_pdf(data, page_number, hide_notify=False):
    output = BytesIO()
    with Image(blob=data, resolution=150) as pdf:
        with Image(width=pdf.width, height=pdf.height) as image:
            try:
                page = pdf.sequence[page_number - 1]
            except IndexError:
                abort(400, 'Letter does not have a page {}'.format(page_number))

            if pdf.colorspace == 'cmyk':
                image.transform_colorspace('cmyk')

            image.composite(page, top=0, left=0)
            if hide_notify:
                hide_notify_tag(image)
            converted = image.convert('png')
            converted.save(file=output)

    output.seek(0)

    return {
        'filename_or_fp': output,
        'mimetype': 'image/png',
    }


def png_data_from_pdf(data, page_number, hide_notify=False):
    return png_from_pdf(
        data, page_number=page_number, hide_notify=hide_notify
    )['filename_or_fp'].getvalue()


@statsd(namespace="template_preview")
def get_logo(dvla_org_id):
    try:
        return current_app.config['LOGOS'][dvla_org_id]
    except KeyError:
        abort(400)


@statsd(namespace="template_preview")
def get_page_count(pdf_data):
    with Image(blob=pdf_data) as image:
        return len(image.sequence)


@preview_blueprint.route("/preview.json", methods=['POST'])
@auth.login_required
@statsd(namespace="template_preview")
def page_count():
    return jsonify(
        {
            'count': get_page_count(
                view_letter_template(filetype='pdf').get_data()
            )
        }
    )


@statsd(namespace="template_preview")
def get_pdf_redis_key(json):
    def print_dict(d):
        """
        From any environment/system will return the same string representation of a given dict.
        """
        return sorted(d.items())

    unique_name_dict = {
        'template_id': json['template']['id'],
        'version': json['template']['version'],
        'dvla_org_id': json['dvla_org_id'],
        'letter_contact_block': json['letter_contact_block'],
        'values': None if not json['values'] else print_dict(json['values'])
    }

    return print_dict(unique_name_dict)


@preview_blueprint.route("/preview.<filetype>", methods=['POST'])
@auth.login_required
@statsd(namespace="template_preview")
def view_letter_template(filetype):
    """
    POST /preview.pdf with the following json blob
    {
        "letter_contact_block": "contact block for service, if any",
        "template": {
            "template data, as it comes out of the database"
        },
        "values": {"dict of placeholder values"},
        "dvla_org_id": {"type": "string"}
    }
    """
    try:
        if filetype not in ('pdf', 'png'):
            abort(404)

        if filetype == 'pdf' and request.args.get('page') is not None:
            abort(400)

        json = get_and_validate_json_from_request(request, preview_schema)
        logo_file_name = get_logo(json['dvla_org_id']).raster

        unique_name = get_pdf_redis_key(json)
        pdf = current_app.redis_store.get(unique_name)

        if not pdf:
            template = LetterPreviewTemplate(
                json['template'],
                values=json['values'] or None,
                contact_block=json['letter_contact_block'],
                # we get the images of our local server to keep network topography clean,
                # which is just http://localhost:6013
                admin_base_url='http://localhost:6013',
                logo_file_name=logo_file_name,
                date=dateutil.parser.parse(json['date']) if json.get('date') else None,
            )
            string = str(template)
            html = HTML(string=string)
            pdf = html.write_pdf()
            current_app.redis_store.set(unique_name, pdf, ex=current_app.config['EXPIRE_CACHE_IN_SECONDS'])

        if filetype == 'pdf':
            return current_app.response_class(pdf, mimetype='application/pdf')
        elif filetype == 'png':
            return send_file(**png_from_pdf(
                pdf, page_number=int(request.args.get('page', 1))
            ))

    except Exception as e:
        current_app.logger.error(str(e))
        raise e


def get_pdf(html):

    @cache(html, 'pdf')
    def _get():
        return HTML(string=html).write_pdf()

    return _get()


@preview_blueprint.route("/preview.html.pdf", methods=['POST'])
@auth.login_required
@statsd(namespace="template_preview")
def view_letter_template_as_pdf():
    """
    POST /preview.html.pdf with the following body
    {
        "html": "string of HTML to be rendered as a PDF/PNG",
    }
    """

    try:
        return current_app.response_class(
            get_pdf(get_html_from_request(request)),
            mimetype='application/pdf',
        )
    except Exception as e:
        current_app.logger.error(str(e))
        raise e


def get_png(html, page_number):

    @cache(html, 'png', str(page_number))
    def _get():
        return png_data_from_pdf(
            view_letter_template_as_pdf().get_data(),
            page_number=page_number,
        )

    return BytesIO(_get())


@preview_blueprint.route("/preview.html.png", methods=['POST'])
@auth.login_required
@statsd(namespace="template_preview")
def view_letter_template_as_png():
    """
    POST /preview.html.png with the following body
    {
        "html": "string of HTML to be rendered as a PNG",
    }
    """

    try:
        return send_file(
            filename_or_fp=get_png(
                get_html_from_request(request),
                int(request.args.get('page', 1)),
            ),
            mimetype='image/png',
        )
    except Exception as e:
        current_app.logger.error(str(e))
        raise e


def get_png_from_precompiled(encoded_string, page_number, hide_notify):

    @cache(encoded_string.decode('ascii'), str(page_number), str(hide_notify))
    def _get():
        pdf = base64.decodestring(encoded_string)
        return png_data_from_pdf(
            pdf,
            page_number=page_number,
            hide_notify=hide_notify,
        )

    return BytesIO(_get())


@preview_blueprint.route("/precompiled-preview.png", methods=['POST'])
@auth.login_required
@statsd(namespace="template_preview")
def view_precompiled_letter():
    try:
        encoded_string = request.get_data()

        if not encoded_string:
            abort(400)

        return send_file(
            filename_or_fp=get_png_from_precompiled(
                encoded_string,
                int(request.args.get('page', 1)),
                hide_notify=request.args.get('hide_notify', '') == 'true',
            ),
            mimetype='image/png',
        )

    # catch invalid pdfs
    except MissingDelegateError as e:
        current_app.logger.warn("Failed to generate PDF", str(e))
        abort(400)

    except Exception as e:
        current_app.logger.error(str(e))
        raise e


@preview_blueprint.route("/print.pdf", methods=['POST'])
@auth.login_required
@statsd(namespace="template_preview")
def print_letter_template():
    """
    POST /print.pdf with the following json blob
    {
        "letter_contact_block": "contact block for service, if any",
        "template": {
            "template data, as it comes out of the database"
        }
        "values": {"dict of placeholder values"},
        "dvla_org_id": {"type": "string"}
    }
    """
    try:
        json = get_and_validate_json_from_request(request, preview_schema)
        logo = get_logo(json['dvla_org_id']).vector

        template = LetterPrintTemplate(
            json['template'],
            values=json['values'] or None,
            contact_block=json['letter_contact_block'],
            # we get the images of our local server to keep network topography clean,
            # which is just http://localhost:6013
            admin_base_url='http://localhost:6013',
            logo_file_name=logo,
        )
        html = HTML(string=str(template))
        pdf = html.write_pdf()

        cmyk_pdf = convert_pdf_to_cmyk(pdf)

        response = send_file(
            BytesIO(cmyk_pdf),
            as_attachment=True,
            attachment_filename='print.pdf'
        )

        response.headers['X-pdf-page-count'] = get_page_count(pdf)
        return response

    except Exception as e:
        current_app.logger.error(str(e))
        raise e


@preview_blueprint.route("/print.html.pdf", methods=['POST'])
@auth.login_required
@statsd(namespace="template_preview")
def print_letter_template_from_html():
    """
    POST /print.pdf with the following json blob
    {
        "html": "string of HTML to render as a PDF",
    }
    """
    try:

        pdf = HTML(string=get_html_from_request(request)).write_pdf()
        cmyk_pdf = convert_pdf_to_cmyk(pdf)

        response = send_file(
            BytesIO(cmyk_pdf),
            as_attachment=True,
            attachment_filename='print.pdf'
        )

        response.headers['X-pdf-page-count'] = get_page_count(pdf)
        return response

    except Exception as e:
        current_app.logger.error(str(e))
        raise e


@preview_blueprint.route("/logos.pdf", methods=['GET'])
# No auth on this endpoint to make debugging easier
@statsd(namespace="template_preview")
def print_logo_sheet():

    html = HTML(string="""
        <html>
            <head>
            </head>
            <body>
                <h1>All letter logos</h1>
                {}
            </body>
        </html>
    """.format('\n<br><br>'.join(
        '<img src="/static/images/letter-template/{}" width="100%">'.format(logo.vector)
        for org_id, logo in current_app.config['LOGOS'].items()
    )))

    pdf = html.write_pdf()
    cmyk_pdf = convert_pdf_to_cmyk(pdf)

    return send_file(
        BytesIO(cmyk_pdf),
        as_attachment=True,
        attachment_filename='print.pdf'
    )


@preview_blueprint.route("/logos.json", methods=['GET'])
@auth.login_required
@statsd(namespace="template_preview")
def get_available_logos():
    return jsonify({
        key: logo.raster
        for key, logo in current_app.config['LOGOS'].items()
    })
