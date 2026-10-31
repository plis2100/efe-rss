from __future__ import annotations
import email.utils
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup

BASE = 'https://efe.com'
RSS = Path('rss.xml')
MAX_ITEMS = 1500
# Listados públicos: titulares, enlaces, extractos y fechas sin abrir cada artículo.
SECCIONES = [
    ('/economia/', 'Economía y empresas'),
    ('/portada-espana/', 'España'),
    ('/mundo/', 'Mundo'),
    ('/europa/', 'Europa'),
    ('/cultura/', 'Cultura'),
    ('/ciencia-y-tecnologia/', 'Ciencia y tecnología'),
    ('/deportes/', 'Deportes'),
    ('/educacion/', 'Educación'),
    ('/salud/', 'Salud'),
    ('/medio-ambiente/', 'Medio ambiente'),
]
# Una sola página por sección para minimizar peticiones; se puede ampliar después.
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; EFE-public-listing-RSS/1.0)',
           'Accept': 'text/html,application/xhtml+xml;q=0.9,*/*;q=0.8',
           'Accept-Language': 'es-ES,es;q=0.9'}
MESES = {'enero':1,'febrero':2,'marzo':3,'abril':4,'mayo':5,'junio':6,'julio':7,'agosto':8,'septiembre':9,'setiembre':9,'octubre':10,'noviembre':11,'diciembre':12}


def descargar(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=22) as r:
        return r.read()


def parse_fecha(texto):
    m = re.search(r'\b(\d{1,2})\s+([a-záéíóú]+)\s+(20\d{2})\b', texto.lower())
    if not m or m.group(2) not in MESES:
        return None
    return datetime(int(m.group(3)), MESES[m.group(2)], int(m.group(1)), tzinfo=timezone.utc)


def fecha_item(item):
    try:
        return email.utils.parsedate_to_datetime(item.findtext('pubDate', '')).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


def extraer_listado(html_bytes, seccion):
    soup = BeautifulSoup(html_bytes, 'html.parser')
    encontrados = {}
    # Los listados de EFE publican los artículos bajo encabezados h2 enlazados.
    for h in soup.select('h2'):
        a = h.find('a', href=True)
        if not a:
            continue
        link = urljoin(BASE, a['href']).split('#',1)[0].split('?',1)[0]
        ruta = urlparse(link).path
        if urlparse(link).netloc not in ('efe.com','www.efe.com') or not re.search(r'/20\d{2}-\d{2}-\d{2}/', ruta):
            continue
        titulo = a.get_text(' ', strip=True)
        if not titulo or len(titulo) < 12:
            continue
        # Fecha desde la URL del propio artículo: no usar fecha de ejecución.
        match = re.search(r'/((?:20\d{2})-\d{2}-\d{2})/', ruta)
        try:
            dt = datetime.strptime(match.group(1), '%Y-%m-%d').replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            continue
        # Los listados contienen una sinopsis próxima al titular.
        contenedor = h.parent
        descripcion = ''
        for _ in range(3):
            if contenedor is None:
                break
            p = contenedor.find('p')
            if p:
                descripcion = p.get_text(' ', strip=True)
                break
            contenedor = contenedor.parent
        if len(descripcion) > 450:
            descripcion = descripcion[:447].rsplit(' ',1)[0] + '…'
        encontrados[link] = (titulo, dt, seccion, descripcion)
    return encontrados


def escribir_atomico(texto):
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', delete=False, dir='.', suffix='.tmp') as f:
        f.write(texto)
        temporal = f.name
    os.replace(temporal, RSS)


def main():
    anteriores = {}
    if RSS.exists():
        try:
            root = ET.parse(RSS).getroot()
            for item in root.findall('./channel/item'):
                link = item.findtext('link', '').strip()
                if link:
                    anteriores[link.split('?',1)[0]] = item
        except ET.ParseError as exc:
            raise SystemExit(f'RSS anterior inválido; no se sobrescribe: {exc}')
    print(f'Noticias anteriores conservadas: {len(anteriores)}', flush=True)
    nuevos = {}
    exitos = 0
    bloqueado = False
    for ruta, seccion in SECCIONES:
        if bloqueado:
            break
        url = urljoin(BASE, ruta)
        try:
            datos = extraer_listado(descargar(url), seccion)
            exitos += 1
            nuevos.update(datos)
            print(f'{seccion}: {len(datos)} titulares verificables', flush=True)
        except urllib.error.HTTPError as exc:
            print(f'{seccion}: HTTP {exc.code}; se conserva el RSS anterior', flush=True)
            if exc.code in (403,429):
                bloqueado = True
        except Exception as exc:
            print(f'{seccion}: fallo de lectura ({exc})', flush=True)
        time.sleep(2)
    if not exitos:
        raise SystemExit('No se pudo consultar ningún listado. RSS anterior intacto.')
    agregados = 0
    for link, (titulo, dt, seccion, descripcion) in nuevos.items():
        if link in anteriores:
            continue
        item = ET.Element('item')
        ET.SubElement(item,'title').text = titulo
        ET.SubElement(item,'link').text = link
        ET.SubElement(item,'guid', {'isPermaLink':'true'}).text = link
        ET.SubElement(item,'pubDate').text = email.utils.format_datetime(dt)
        ET.SubElement(item,'category').text = seccion
        if descripcion:
            ET.SubElement(item,'description').text = descripcion
        anteriores[link] = item
        agregados += 1
    print(f'Titulares detectados en listados: {len(nuevos)}', flush=True)
    print(f'Nuevas añadidas: {agregados}', flush=True)
    if not agregados:
        print('Sin novedades verificables; no se modifica rss.xml.', flush=True)
        return
    ordenados = sorted(anteriores.values(), key=fecha_item, reverse=True)[:MAX_ITEMS]
    rss = ET.Element('rss', {'version':'2.0'})
    canal = ET.SubElement(rss,'channel')
    ET.SubElement(canal,'title').text = 'EFE — Noticias públicas'
    ET.SubElement(canal,'link').text = BASE
    ET.SubElement(canal,'description').text = 'Titulares publicados en listados públicos de EFE; cobertura parcial, no exhaustiva.'
    ET.SubElement(canal,'language').text = 'es-ES'
    ET.SubElement(canal,'lastBuildDate').text = email.utils.format_datetime(datetime.now(timezone.utc))
    for item in ordenados:
        canal.append(item)
    ET.indent(rss, space='  ')
    escribir_atomico(ET.tostring(rss, encoding='unicode', xml_declaration=True) + '\n')
    print(f'RSS actualizado: {len(ordenados)} noticias', flush=True)
    print('estado.json conservado; las URLs nuevas quedan deduplicadas en rss.xml.', flush=True)

if __name__ == '__main__':
    main()
