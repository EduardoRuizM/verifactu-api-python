#
# Veri*Factu - 2025 Eduardo Ruiz <eruiz@dataclick.es>
# https://github.com/EduardoRuizM/verifactu-api-python
#

import re
import urllib.parse

from flask import jsonify
from sqlalchemy import text
from http import HTTPStatus
from datetime import datetime
from sqlalchemy.dialects.mysql import INTEGER

from app import db


class Company(db.Model):
    __tablename__ = 'companies'
    id = db.Column(INTEGER(unsigned=True), primary_key=True, autoincrement=True)
    name = db.Column(db.String(25), unique=True, nullable=False)
    vat_id = db.Column(db.String(25), unique=True, nullable=False)
    address = db.Column(db.String(75))
    postal_code = db.Column(db.String(10))
    city = db.Column(db.String(25))
    state = db.Column(db.String(25))
    country = db.Column(db.String(2), default='ES', server_default=text("'ES'"))
    email = db.Column(db.String(50))
    phone = db.Column(db.String(50))
    contact = db.Column(db.String(50))
    formula = db.Column(db.String(25), default='%y%/%n.8%', server_default=text("'%y%/%n.8%'"))
    formula_r = db.Column(db.String(25), default='R-%y%/%n.8%', server_default=text("'R-%y%/%n.8%'"))
    first_num = db.Column(INTEGER(unsigned=True), default='1', server_default=text("'1'"))
    created = db.Column(db.Date, nullable=False, default=db.func.now())
    key_file = db.Column(db.String(200))
    cert_file = db.Column(db.String(200))
    next_send = db.Column(db.DateTime)
    test = db.Column(db.Boolean, nullable=False, default=True, server_default='1')

    def __repr__(self):
        return f'<Company {self.name}>'

    def to_dict(self, cert_days=False):
        result = to_dict(self)
        result['created'] = self.created.strftime('%Y-%m-%d')
        result.pop('key_file', None)
        result.pop('cert_file', None)
        return result

    @staticmethod
    def validate_fields(data, element=None):
        required = ['name', 'vat_id']
        allowed = ['name', 'vat_id', 'address', 'postal_code', 'city', 'state', 'country', 'email', 'phone', 'contact', 'test']
        return validate_fields(data, required, allowed, element)

    def get_url_aeat(self):
        return 'https://prewww2.aeat.es/' if self.test else 'https://www2.agenciatributaria.gob.es/'


class Invoice(db.Model):
    __tablename__ = 'invoices'
    id = db.Column(INTEGER(unsigned=True), primary_key=True, autoincrement=True)
    company_id = db.Column(INTEGER(unsigned=True), db.ForeignKey('companies.id', ondelete='RESTRICT'), nullable=False)
    dt = db.Column(db.DateTime, index=True, nullable=False, default=db.func.current_timestamp())
    num = db.Column(INTEGER(unsigned=True), index=True, nullable=False)
    name = db.Column(db.String(50), nullable=False)
    vat_id = db.Column(db.String(25))
    address = db.Column(db.String(75))
    postal_code = db.Column(db.String(10))
    city = db.Column(db.String(25))
    state = db.Column(db.String(25))
    country = db.Column(db.String(2), default='ES', server_default=text("'ES'"))
    tvat = db.Column(db.Float, nullable=False, default=0.0)
    bi = db.Column(db.Float, nullable=False, default=0.0)
    total = db.Column(db.Float, nullable=False, default=0.0)
    email = db.Column(db.String(50))
    ref = db.Column(db.String(25))
    comments = db.Column(db.Text)
    fingerprint = db.Column(db.String(64), index=True)
    verifactu_type = db.Column(db.String(2), index=True)
    verifactu_stype = db.Column(db.String(1))
    verifactu_dt = db.Column(db.TIMESTAMP, index=True)
    verifactu_csv = db.Column(db.Text)
    verifactu_err = db.Column(INTEGER(unsigned=True))
    invoice_ref_id = db.Column(INTEGER(unsigned=True), db.ForeignKey('invoices.id', ondelete='RESTRICT'))
    voided = db.Column(db.Boolean, index=True, nullable=False, default=False, server_default='0')

    company = db.relationship('Company', backref='invoices')
    invoice_ref = db.relationship('Invoice', backref=db.backref('invoice_refs', remote_side=[id]))

    def __repr__(self):
        return f'<Invoice {self.id}>'

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if not self.num:
            self.get_next_num()

    def to_dict(self):
        result = to_dict(self)
        result['dt'] = self.dt.strftime('%Y-%m-%d %H:%M:%S')
        result['verifactu_dt'] = self.verifactu_dt.strftime('%Y-%m-%d %H:%M:%S') if self.verifactu_dt else None
        result['invoice_ref'] = self.invoice_ref.get_number_format() if self.invoice_ref else None
        result['number_format'] = self.get_number_format()
        return result

    def to_lines_dict(self):
        return {**self.to_dict(), 'lines': [line.to_dict() for line in self.invoice_lines]}

    @staticmethod
    def validate_fields(data, element=None):
        required = ['company_id', 'name']
        allowed = ['company_id', 'name', 'vat_id', 'address', 'postal_code', 'city', 'state',
                   'vat', 'email', 'ref', 'comments', 'verifactu_type', 'verifactu_stype']
        return validate_fields(data, required, allowed, element)

    def get_next_num(self):
        current_year = datetime.now().year
        filters = [
            Invoice.company_id == self.company_id,
            db.extract('year', Invoice.dt) == current_year
        ]

        if getattr(self, 'verifactu_type', None):
            f = 'R' if self.verifactu_type and self.verifactu_type.startswith('R') else 'F'
            filters.append(Invoice.verifactu_type.startswith(f))

        max_num = db.session.query(db.func.max(Invoice.num)).filter(*filters).scalar()
        self.num = (max_num + 1) if max_num else getattr(self.company, 'first_num', 1)

    def get_number_format(self):
        f = 'formula' if not self.verifactu_type or self.verifactu_type[0] == 'F' else 'formula_r'
        formula = getattr(self.company, f, None) or ('%n%' if self.verifactu_type[0] == 'F' else 'R-%n%')
        return re.sub(r'%n(?:\.(\d+))?%',
            lambda m: f'{self.num:0{int(m.group(1))}d}' if m.group(1) else str(self.num),
            formula.replace('%y%', self.dt.strftime('%y')).replace('%Y%', self.dt.strftime('%Y')))

    def get_verifactu_qr(self):
        return self.company.get_url_aeat() + 'wlpl/TIKE-CONT/ValidarQR?nif=' + urllib.parse.quote(self.company.vat_id) +\
               '&numserie=' + urllib.parse.quote(self.get_number_format()) + '&fecha=' +\
               urllib.parse.quote(self.dt.strftime('%d-%m-%Y')) + '&importe=' + urllib.parse.quote(f'{float(self.total):.2f}')

    def get_number(self, value, default=0):
        value = str(value).replace(',', '.')
        return float(value) if value.replace('.', '', 1).isdigit() else default

    def process_lines(self, data):
        num = 0
        self.tvat = 0
        self.bi = 0
        self.total =  0
        for line in data['lines']:
            ret, status = InvoiceLine.validate_fields(line)
            if status:
                db.session.rollback()
                return ret, status

            num = num + 1
            invoice_line = InvoiceLine(**ret)
            invoice_line.invoice_id = self.id
            invoice_line.num = num
            units = self.get_number(line['units'], 1)
            price = self.get_number(line['price'])
            vat = self.get_number(line['vat'])
            invoice_line.vat = vat if vat else None
            invoice_line.bi = round(units * price, 2)
            invoice_line.tvat = round(invoice_line.bi * ((vat or 0) / 100), 2)
            invoice_line.total = round(invoice_line.bi + invoice_line.tvat, 2)
            self.bi += invoice_line.bi
            self.tvat += invoice_line.tvat
            self.total += invoice_line.total
            db.session.add(invoice_line)
        db.session.commit()
        return None, None


class InvoiceLine(db.Model):
    __tablename__ = 'invoice_lines'
    invoice_id = db.Column(INTEGER(unsigned=True), db.ForeignKey('invoices.id', ondelete='CASCADE'), primary_key=True)
    num = db.Column(INTEGER(unsigned=True), primary_key=True, default=1)
    descr = db.Column(db.String(100))
    units = db.Column(db.Integer)
    price = db.Column(db.Float)
    vat = db.Column(INTEGER(unsigned=True))
    tvat = db.Column(db.Float)
    bi = db.Column(db.Float)
    total = db.Column(db.Float)

    invoice = db.relationship('Invoice', backref=db.backref('invoice_lines', order_by='InvoiceLine.num'))

    def to_dict(self):
        return to_dict(self)

    @staticmethod
    def validate_fields(data, element=None):
        required = ['descr', 'units', 'price']
        allowed = ['descr', 'units', 'price', 'vat']
        return validate_fields(data, required, allowed, element)


def to_dict(obj):
    return {k: (v.to_dict() if hasattr(v, '__tablename__') else v) for k, v in vars(obj).items() if not k.startswith('_')}


def validate_fields(data, required_fields, allowed_fields, element=None):
    if data is None:
        return jsonify({'error': 'No JSON'}), HTTPStatus.UNSUPPORTED_MEDIA_TYPE

    if element:
        for field in required_fields:
            if field in data and not data[field]:
                return jsonify({'error': f'Missing {field}'}), HTTPStatus.BAD_REQUEST
    else:
        for field in required_fields:
            if field not in data:
                return jsonify({'error': f'Missing {field}'}), HTTPStatus.BAD_REQUEST

    if element:
        for key, value in data.items():
            if key in allowed_fields and hasattr(element, key):
                setattr(element, key, value)
        return None, None
    else:
        return {key: value for key, value in data.items() if key in allowed_fields}, None
