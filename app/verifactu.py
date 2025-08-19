#
# Veri*Factu - 2025 Eduardo Ruiz <eruiz@dataclick.es>
# https://github.com/EduardoRuizM/verifactu-api-python
#

import os
import re
import ssl
import sys
import hashlib
import configparser
import urllib.request
import xml.etree.ElementTree as ET

from datetime import datetime
from sqlalchemy import desc, update
from configparser import UNNAMED_SECTION

from app import db, config_file, time_zone
from .models import Company, Invoice, InvoiceLine


class verifactuXML:
    def __init__(self):
        config = configparser.ConfigParser(allow_unnamed_section=True)
        config.read(config_file)

        self.log_file = config.get(UNNAMED_SECTION, 'verifactu_log_file', fallback='')
        self.save_responses = config.get(UNNAMED_SECTION, 'verifactu_save_responses', fallback='')
        self.software_company_name = config.get(UNNAMED_SECTION, 'software_company_name', fallback='')
        self.software_company_nif = config.get(UNNAMED_SECTION, 'software_company_nif', fallback='')
        self.software_name = config.get(UNNAMED_SECTION, 'software_name', fallback='')
        self.software_id = config.get(UNNAMED_SECTION, 'software_id', fallback='vf')[:2]
        self.software_version = config.get(UNNAMED_SECTION, 'software_version', fallback='1.0')
        self.software_install_number = config.get(UNNAMED_SECTION, 'software_install_number', fallback='00001')

        self.url_prod = 'https://www1.agenciatributaria.gob.es/wlpl/TIKE-CONT/ws/SistemaFacturacion/VerifactuSOAP'
        self.url_test = 'https://prewww1.aeat.es/wlpl/TIKE-CONT/ws/SistemaFacturacion/VerifactuSOAP'

        if not self.software_company_name or not self.software_company_nif or not self.software_name or not self.software_id or not self.software_version or not self.software_install_number:
            sys.exit('Software info not found')

    def cur(self, num):
        return f'{float(num):.2f}'

    def dt(self, invoice):
        return invoice.dt.strftime('%d-%m-%Y')

    def cod(self, string):
        return ''.join(filter(str.isalnum, string)).upper().strip()

    def log(self, message):
        if self.log_file:
            with open(self.log_file, 'a', encoding='utf-8') as log_file:
                log_file.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {message}\n")

    def hour_timezone(self):
        offset = 0
        if time_zone in ['Europe/Madrid', 'Atlantic/Canary']:
            year = datetime.now().year
            last_sunday_march = max(datetime(year, 3, day) for day in range(31, 24, -1) if datetime(year, 3, day).weekday() == 6)
            last_sunday_october = max(datetime(year, 10, day) for day in range(31, 24, -1) if datetime(year, 10, day).weekday() == 6)
            now = datetime.now()
            offset = 2 if last_sunday_march <= now < last_sunday_october else 1
            if time_zone == 'Atlantic/Canary':
                offset -= 1
        now = datetime.now()
        return now.strftime(f"%Y-%m-%dT%H:%M:%S{('+%02d:00' % offset) if offset >= 0 else ('%02d:00' % offset)}")

    def last_invoice(self, company):
        return db.session.query(Invoice).filter(
            Invoice.company_id == company.id,
            Invoice.fingerprint.isnot(None)
        ).order_by(desc(Invoice.verifactu_dt), desc(Invoice.id)).first()

    def fingerprint(self, company, invoice, last, dt, voided=False):
        last_fp = last.fingerprint if last is not None else ''

        if voided:
            f = (f"IDEmisorFacturaAnulada={self.cod(company.vat_id)}&NumSerieFacturaAnulada={invoice.get_number_format()}"
            f"&FechaExpedicionFacturaAnulada={self.dt(invoice)}&Huella={last_fp}&FechaHoraHusoGenRegistro={dt}")
        else:
            f = (f"IDEmisorFactura={self.cod(company.vat_id)}&NumSerieFactura={invoice.get_number_format()}"
            f"&FechaExpedicionFactura={self.dt(invoice)}&TipoFactura={invoice.verifactu_type}"
            f"&CuotaTotal={self.cur(invoice.tvat)}&ImporteTotal={self.cur(invoice.total)}&Huella={last_fp}&FechaHoraHusoGenRegistro={dt}")
        return hashlib.sha256(f.encode()).hexdigest().upper()

    def registro_alta(self, company, invoice, last, dt):
        if not invoice.comments:
            line = db.session.query(InvoiceLine).filter(InvoiceLine.invoice_id == invoice.id).first()
            descr = line.descr if line else 'Factura'
        else:
            descr = invoice.comments

        xml = f'<sum:RegistroFactura><RegistroAlta><IDVersion>1.0</IDVersion><IDFactura>'
        xml += f'<IDEmisorFactura>{self.cod(company.vat_id)}</IDEmisorFactura>'
        xml += f'<NumSerieFactura>{invoice.get_number_format()}</NumSerieFactura>'
        xml += f'<FechaExpedicionFactura>{self.dt(invoice)}</FechaExpedicionFactura></IDFactura>'
        xml += f'<NombreRazonEmisor>{company.name}</NombreRazonEmisor>'

        if invoice.verifactu_err is not None:
            xml += f'<Subsanacion>S</Subsanacion>'
            xml += f'<RechazoPrevio>X</RechazoPrevio>'

        xml += f'<TipoFactura>{invoice.verifactu_type}</TipoFactura>'

        if invoice.verifactu_type.startswith('R') or invoice.verifactu_type == 'F3':
            if invoice.verifactu_stype:
                xml += f'<TipoRectificativa>{"S" if invoice.verifactu_stype == "S" else "I"}</TipoRectificativa>'

            rinvoices = db.session.query(Invoice).filter(Invoice.invoice_ref_id == invoice.id).order_by(Invoice.dt).all()
            for rinvoice in rinvoices:
                tag1 = '<FacturasSustituidas>' if invoice.verifactu_type == 'F3' else '<FacturasRectificadas>'
                tag2 = '</FacturasSustituidas>' if invoice.verifactu_type == 'F3' else '</FacturasRectificadas>'
                xml += f'{tag1}<IDEmisorFactura>{self.cod(company.vat_id)}</IDEmisorFactura>'
                xml += f'<NumSerieFactura>{rinvoice.get_number_format()}</NumSerieFactura>'
                xml += f'<FechaExpedicionFactura>{self.dt(rinvoice)}</FechaExpedicionFactura>{tag2}'

            if invoice.verifactu_stype == 'S':
                bi_total = 0.0
                tvat_total = 0.0
                for rinvoice in rinvoices:
                    lines = db.session.query(
                        db.func.sum(InvoiceLine.bi).label('bi'),
                        db.func.sum(InvoiceLine.tvat).label('tvat')
                    ).filter(InvoiceLine.invoice_id == rinvoice.id).group_by(InvoiceLine.vat).all()
                    for line in lines:
                        bi_total += float(line.bi or 0)
                        tvat_total += float(line.tvat or 0)
                xml += f'<ImporteRectificacion><BaseRectificada>{self.cur(bi_total)}</BaseRectificada>'
                xml += f'<CuotaRectificada>{self.cur(tvat_total)}</CuotaRectificada></ImporteRectificacion>'

        xml += f'<DescripcionOperacion>{descr}</DescripcionOperacion>'
        if invoice.verifactu_type == 'F2':
            xml += f'<FacturaSimplificadaArt7273>S</FacturaSimplificadaArt7273>'

        if not invoice.vat_id:
            xml += f'<FacturaSinIdentifDestinatarioArt61d>S</FacturaSinIdentifDestinatarioArt61d>'
        else:
            xml += f'<Destinatarios><IDDestinatario><NombreRazon>{invoice.name}</NombreRazon>'
            xml += f'<NIF>{invoice.vat_id}</NIF></IDDestinatario></Destinatarios>'

        xml += f'<Desglose>'
        lines = db.session.query(
            db.func.sum(InvoiceLine.bi).label('bi'),
            db.func.sum(InvoiceLine.tvat).label('tvat'),
            InvoiceLine.vat
        ).filter(InvoiceLine.invoice_id == invoice.id).group_by(InvoiceLine.vat).all()
        for line in lines:
            xml += f'<DetalleDesglose><Impuesto>01</Impuesto>'
            if line.vat:
                xml += f'<ClaveRegimen>01</ClaveRegimen><CalificacionOperacion>S1</CalificacionOperacion>'
                xml += f'<TipoImpositivo>{line.vat}</TipoImpositivo><BaseImponibleOimporteNoSujeto>{self.cur(line.bi)}</BaseImponibleOimporteNoSujeto>'
                xml += f'<CuotaRepercutida>{self.cur(line.tvat)}</CuotaRepercutida>'
            else:
                xml += f'<CalificacionOperacion>N1</CalificacionOperacion>'
                xml += f'<BaseImponibleOimporteNoSujeto>{self.cur(line.bi)}</BaseImponibleOimporteNoSujeto>'
            xml += f'</DetalleDesglose>'

        xml += f'</Desglose><CuotaTotal>{self.cur(invoice.tvat)}</CuotaTotal>'
        xml += f'<ImporteTotal>{self.cur(invoice.total)}</ImporteTotal>'

        xml += f'<Encadenamiento>'
        if last:
            xml += f'<RegistroAnterior><IDEmisorFactura>{self.cod(company.vat_id)}</IDEmisorFactura>'
            xml += f'<NumSerieFactura>{invoice.get_number_format()}</NumSerieFactura>'
            xml += f'<FechaExpedicionFactura>{self.dt(last)}</FechaExpedicionFactura>'
            xml += f'<Huella>{last.fingerprint}</Huella></RegistroAnterior>'
        else:
            xml += f'<PrimerRegistro>S</PrimerRegistro>'

        xml += f'</Encadenamiento>'
        xml += f'{self.sistema_informatico()}'
        xml += f'<FechaHoraHusoGenRegistro>{dt}</FechaHoraHusoGenRegistro>'
        xml += f'<TipoHuella>01</TipoHuella>'
        xml += f'<Huella>{self.fingerprint(company, invoice, last, dt, False)}</Huella>'
        xml += f'</RegistroAlta></sum:RegistroFactura>'

        return xml

    def registro_anulacion(self, company, invoice, last, dt):
        xml = f"""<sum:RegistroFactura>
                    <RegistroAnulacion>
                        <IDVersion>1.0</IDVersion>
                        <IDFactura>
                            <IDEmisorFacturaAnulada>{self.cod(company.vat_id)}</IDEmisorFacturaAnulada>
                            <NumSerieFacturaAnulada>{invoice.get_number_format()}</NumSerieFacturaAnulada>
                            <FechaExpedicionFacturaAnulada>{self.dt(invoice)}</FechaExpedicionFacturaAnulada>
                        </IDFactura>"""

        if invoice.verifactu_err > 0:
            xml += "<RechazoPrevio>S</RechazoPrevio>"

        xml += "<Encadenamiento>"

        if last:
            xml += f"""<RegistroAnterior>
                            <IDEmisorFactura>{self.cod(company.vat_id)}</IDEmisorFactura>
                            <NumSerieFactura>{invoice.get_number_format()}</NumSerieFactura>
                            <FechaExpedicionFactura>{self.dt(last)}</FechaExpedicionFactura>
                            <Huella>{last.fingerprint}</Huella>
                        </RegistroAnterior>"""
        else:
            xml += "<PrimerRegistro>S</PrimerRegistro>"

        xml += f"""</Encadenamiento>
                {self.sistema_informatico()}
                <FechaHoraHusoGenRegistro>{dt}</FechaHoraHusoGenRegistro>
                <TipoHuella>01</TipoHuella><Huella>{self.fingerprint(company, invoice, last, dt, True)}</Huella>
                </RegistroAnulacion>
            </sum:RegistroFactura>"""

        return xml

    def sistema_informatico(self):
        return f"""<SistemaInformatico>
                        <NombreRazon>{self.software_company_name}</NombreRazon>
                        <NIF>{self.software_company_nif}</NIF>
                        <NombreSistemaInformatico>{self.software_name}</NombreSistemaInformatico>
                        <IdSistemaInformatico>{self.software_id}</IdSistemaInformatico>
                        <Version>{self.software_version}</Version>
                        <NumeroInstalacion>{self.software_install_number}</NumeroInstalacion>
                        <TipoUsoPosibleSoloVerifactu>N</TipoUsoPosibleSoloVerifactu>
                        <TipoUsoPosibleMultiOT>S</TipoUsoPosibleMultiOT>
                        <IndicadorMultiplesOT>S</IndicadorMultiplesOT>
                   </SistemaInformatico>"""

    def pending(self):
        resp = {'companies': {}}

        companies = db.session.query(Company,
            (db.func.unix_timestamp(Company.next_send) - db.func.unix_timestamp(db.func.now())).label('nxSend')
        ).all()

        for company, nx_send in companies:
            resp['companies'][company.id] = {}

            if nx_send and nx_send > 0:
                resp['companies'][company.id]['message'] = f'Next send in {nx_send} seconds'
            else:
                invoices = db.session.query(Invoice).filter(
                    Invoice.company_id == company.id,
                    Invoice.verifactu_dt.is_(None)
                ).order_by(Invoice.dt).limit(1000).all()

                resp['companies'][company.id] = self.send(company, invoices)

        return resp

    def voided(self, company, invoices):
        return self.send(company, invoices, True)

    def send(self, company, invoices, voided=False):
        if not invoices or len(invoices) == 0:
            return {'message': 'No invoices to send'}

        ikeys = {}
        for key, invoice in enumerate(invoices):
            ikeys[invoice.get_number_format()] = key

        dt = self.hour_timezone()
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
                    <soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                            xmlns:sum="https://www2.agenciatributaria.gob.es/static_files/common/internet/dep/aplicaciones/es/aeat/tike/cont/ws/SuministroLR.xsd"
                            xmlns="https://www2.agenciatributaria.gob.es/static_files/common/internet/dep/aplicaciones/es/aeat/tike/cont/ws/SuministroInformacion.xsd">
                        <soapenv:Header/>
                        <soapenv:Body>
                            <sum:RegFactuSistemaFacturacion>
                                <sum:Cabecera>
                                    <ObligadoEmision>
                                        <NombreRazon>{company.name}</NombreRazon>
                                        <NIF>{self.cod(company.vat_id)}</NIF>
                                    </ObligadoEmision>
                                </sum:Cabecera>"""

        last = self.last_invoice(company)

        for invoice in invoices:
            if voided:
                xml += self.registro_anulacion(company, invoice, last, dt)
            else:
                xml += self.registro_alta(company, invoice, last, dt)

        xml += '''    </sum:RegFactuSistemaFacturacion>
                    </soapenv:Body>
                </soapenv:Envelope>'''

        if self.save_responses and os.path.isdir(self.save_responses):
            file_path = os.path.join(self.save_responses.rstrip('/'), f'send_{self.dtnow()}.xml')
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(xml)

        ret = self.send_xml(company, xml)
        if ret.get('status') != 200 or ret.get('error'):
            return ret

        try:
            root = ET.fromstring(ret['response'])
            body = root.find('.//{http://schemas.xmlsoap.org/soap/envelope/}Body')
            if body is None:
                raise ValueError('No body')
            namespaces = {
                'tikR': 'https://www2.agenciatributaria.gob.es/static_files/common/internet/dep/aplicaciones/es/aeat/tike/cont/ws/RespuestaSuministro.xsd',
                'tik': 'https://www2.agenciatributaria.gob.es/static_files/common/internet/dep/aplicaciones/es/aeat/tike/cont/ws/SuministroInformacion.xsd'
            }
            lines = body.findall(f'.//{{{namespaces["tikR"]}}}RespuestaLinea')
            if not isinstance(lines, list):
                lines = [lines]
            if not lines:
                raise ValueError('No lines')
        except (ET.ParseError, KeyError, TypeError, ValueError) as e:
            self.log(f'XML error={str(e)}')
            return {'error': f'XML error={str(e)}'}

        ret = {'ok': [], 'ko': []}
        csv = self.get_text(body.find(f'.//{{{namespaces["tikR"]}}}CSV'))
        tiempo_espera_envio = self.get_text(body.find(f'.//{{{namespaces["tikR"]}}}TiempoEsperaEnvio'))
        timestamp_presentacion = self.get_text(body.find(f'.//{{{namespaces["tikR"]}}}DatosPresentacion/{{{namespaces["tik"]}}}TimestampPresentacion'))

        db.session.query(Company).filter_by(id=company.id).update({
            'next_send': db.func.DATE_ADD(db.func.NOW(), db.text(f"INTERVAL {tiempo_espera_envio} SECOND"))
        })
        db.session.commit()

        for line in lines:
            num_serie_factura = self.get_text(line.find(f'.//{{{namespaces["tikR"]}}}IDFactura/{{{namespaces["tik"]}}}NumSerieFactura'))
            cod_error = self.get_text(line.find(f'.//{{{namespaces["tikR"]}}}CodigoErrorRegistro'), 0)
            descr_error = self.get_text(line.find(f'.//{{{namespaces["tikR"]}}}DescripcionErrorRegistro'))

            index = ikeys.get(num_serie_factura)
            if index is None:
                ret['ko'].append({'num': num_serie_factura, 'codError': 'Not exists'})
                continue
            invoice = invoices[index]

            stmt = update(Invoice).where(Invoice.id == invoice.id).values({
                'verifactu_dt': timestamp_presentacion if timestamp_presentacion else dt,
                'verifactu_err': cod_error
            })
            if csv:
                verifactu_csv = invoice.verifactu_csv + "\n" + csv if invoice.verifactu_csv else csv
                stmt = stmt.values({'verifactu_csv': verifactu_csv.strip()})
            if timestamp_presentacion:
                stmt = stmt.values({'fingerprint': self.fingerprint(company, invoice, last, timestamp_presentacion, voided)})
            if not cod_error and voided:
                stmt = stmt.values({'voided': 1})
            db.session.execute(stmt)
            db.session.commit()

            if cod_error:
                ret['ko'].append({'id': invoice.id, 'num': num_serie_factura, 'codError': cod_error, 'descrError': descr_error})
            else:
                ret['ok'].append({'id': invoice.id, 'num': num_serie_factura})

            log = ''
            items = [
                'tikR:Operacion/tik:TipoOperacion',
                'tikR:EstadoRegistro',
                'tikR:CodigoErrorRegistro',
                'tikR:DescripcionErrorRegistro',
                'tikR:IDFactura/tik:NumSerieFactura',
                'tikR:IDFactura/tik:IDEmisorFactura'
            ]
            for item in items:
                parts = item.split('/')
                xpath = './/'
                for part in parts:
                    prefix, tag = part.split(':')
                    xpath += f'{{{namespaces[prefix]}}}{tag}/'

                value = self.get_text(line.find(xpath.rstrip('/')))
                if value:
                    tag_name = item.split('/')[-1].split(':')[-1]
                    log += f' {tag_name}={value}'

            self.log(log.strip())

        return ret

    def get_text(self, elem, default=None):
        return elem.text if elem is not None else default

    def dom_to_dict(self, element):
        node_dict = {}
        for child in element:
            tag_name = child.tag.split('}')[-1]
            value = self.dom_to_dict(child) if len(child) else child.text
            if tag_name in node_dict:
                if isinstance(node_dict[tag_name], list):
                    node_dict[tag_name].append(value)
                else:
                    node_dict[tag_name] = [node_dict[tag_name], value]
            else:
                node_dict[tag_name] = value
        return node_dict

    def consulta(self, company, year=0, month=0):
        year = max(2025, min(2200, year)) or datetime.now().year
        month = f'{max(1, min(12, month or datetime.now().month)):02d}'

        xml = f'''<?xml version="1.0" encoding="UTF-8"?>
        <soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                xmlns:con="https://www2.agenciatributaria.gob.es/static_files/common/internet/dep/aplicaciones/es/aeat/tike/cont/ws/ConsultaLR.xsd"
                xmlns:sum="https://www2.agenciatributaria.gob.es/static_files/common/internet/dep/aplicaciones/es/aeat/tike/cont/ws/SuministroInformacion.xsd">
            <soapenv:Header/>
            <soapenv:Body>
                <con:ConsultaFactuSistemaFacturacion>
                    <con:Cabecera>
                        <sum:IDVersion>1.0</sum:IDVersion>
                        <sum:ObligadoEmision>
                            <sum:NombreRazon>{company.name}</sum:NombreRazon>
                            <sum:NIF>{self.cod(company.vat_id)}</sum:NIF>
                        </sum:ObligadoEmision>
                    </con:Cabecera>
                    <con:FiltroConsulta>
                        <con:PeriodoImputacion>
                            <sum:Ejercicio>{year}</sum:Ejercicio>
                            <sum:Periodo>{month}</sum:Periodo>
                        </con:PeriodoImputacion>
                    </con:FiltroConsulta>
                </con:ConsultaFactuSistemaFacturacion>
            </soapenv:Body>
        </soapenv:Envelope>'''

        ret = self.send_xml(company, xml, log=False)
        if ret['status'] != 200 or ret['error']:
            return ret

        root = ET.fromstring(ret['response'])
        body = root.find('.//{http://schemas.xmlsoap.org/soap/envelope/}Body')
        if body is None:
            return {'data': []}

        regs = body.findall('.//{https://www2.agenciatributaria.gob.es/static_files/common/internet/dep/aplicaciones/es/aeat/tike/cont/ws/RespuestaConsultaLR.xsd}RegistroRespuestaConsultaFactuSistemaFacturacion')
        data = [self.dom_to_dict(reg) for reg in regs]

        return {'data': data}

    def dtnow(self):
        return datetime.now().strftime('%Y%m%d%H%M%S')

    def send_xml(self, company, xml, log=True):
        error = None
        xml = re.sub(r'>\s+<', '><', re.sub(r'\s*xmlns', ' xmlns', xml))

        url = self.url_test if company.test == 1 else self.url_prod
        context = ssl.create_default_context()
        context.load_cert_chain(certfile=company.cert_file, keyfile=company.key_file)
        req = urllib.request.Request(url, data=xml.encode('utf-8'), headers={'Content-Type': 'text/xml'}, method='POST')

        try:
            with urllib.request.urlopen(req, context=context) as response:
                status = response.getcode()
                response = response.read().decode('utf-8')
        except urllib.error.URLError as e:
            error = str(e)
            status = 400
            response = ''

        if log and self.save_responses and os.path.isdir(self.save_responses):
            filename = os.path.join(self.save_responses, f'resp_{self.dtnow()}.xml')
            with open(filename, 'w', encoding='utf-8') as f:
                f.write(response)

        return {'status': status, 'response': response, 'error': error}
