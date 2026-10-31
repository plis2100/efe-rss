from __future__ import annotations

import email.utils
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

BASE = 'https://efe.com'
INDEX = BASE + '/sitemap_index.xml'
RSS = Path('rss.xml')
ESTADO = Path('estado.json')
NS = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9',
      'image': 'http://www.google.com/schemas/sitemap-image/1.1'}
HEADERS = {'User-Agent': 'RSS-EFE-Monitor/1.0 (public sitemap reader)',
           'Accept': 'application/xml,text/xml;q=0.9,*/*;q=0.8'}
MAX_ITEMS = 1500
SITEMAPS = 3


def get_xml(url):
    for intento in range(3):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=25) as response:
                return ET.fromstring(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or intento == 2:
                raise
            retry = exc.headers.get('Retry-After', '')
            delay = min(int(retry), 30) if retry.isdigit() else 3 * (intento + 1)
            time.sleep(delay)
    raise RuntimeError('No se pudo obtener el XML')


def fecha(valor):
    if not valor:
        return None
    try:
        dt = datetime.fromisoformat(valor.strip().replace('Z', '+00:00'))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except ValueError:
        try:
            dt = email.utils.parsedate_to_datetime(valor)
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
        except (TypeError, ValueError):
            return None


def escribir(ruta, texto):
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', delete=False,
                                     dir='.', prefix='efe_', suffix='.tmp') as f:
        f.write(texto)
        temporal = f.name
    os.replace(temporal, ruta)


def main():
    # Nunca se solicitan páginas individuales de noticias: evita el patrón que provocaba 429.
    try:
        raiz = get_xml(INDEX)
        enlaces = [n.findtext('sm:loc', '', NS).strip() for n in raiz.findall('sm:sitemap', NS)]
        enlaces = [u for u in enlaces if re.search(r'/post-sitemap\d+\.xml$', u)]
        enlaces.sort(key=lambda u: int(re.search(r'post-sitemap(\d+)', u).group(1)), reverse=True)
        if not enlaces:
            raise RuntimeError('El índice no contiene post-sitemaps reconocibles')
        nuevos = {}
        errores = 0
        for url in enlaces[:SITEMAPS]:
            try:
                mapa = get_xml(url)
            except Exception as exc:
                errores += 1
                print(f'No se pudo leer sitemap {url}: {exc}')
                continue
            for nodo in mapa.findall('sm:url', NS):
                link = nodo.findtext('sm:loc', '', NS).strip().split('#')[0].split('?')[0]
                if not link.startswith(BASE + '/') or not re.search(r'/20\d{2}-\d{2}-\d{2}/', urlparse(link).path):
                    continue
                titulo = nodo.findtext('image:image/image:title', '', NS).strip()
                # Sin un título suministrado por la fuente, no inventamos uno a partir del slug.
                if not titulo:
                    continue
                dt = fecha(nodo.findtext('sm:lastmod', '', NS))
                if dt is None:
                    continue
                nuevos[link] = (titulo, dt)
        if errores == min(SITEMAPS, len(enlaces)):
            raise RuntimeError('Todos los sitemaps han fallado: se conserva el RSS existente')
    except Exception as exc:
        print(f'ERROR al consultar EFE: {exc}. No se modifican rss.xml ni estado.json')
        raise SystemExit(1)

    anteriores = {}
    if RSS.exists():
        try:
            root = ET.parse(RSS).getroot()
            for item in root.findall('./channel/item'):
                link = item.findtext('link', '').strip()
                if link:
                    anteriores[link] = item
        except ET.ParseError as exc:
            raise SystemExit(f'RSS anterior inválido; no se sobrescribe: {exc}')

    agregados = 0
    for link, (titulo, dt) in nuevos.items():
        if link in anteriores:
            continue
        item = ET.Element('item')
        ET.SubElement(item, 'title').text = titulo
        ET.SubElement(item, 'link').text = link
        ET.SubElement(item, 'guid', {'isPermaLink': 'true'}).text = link
        # lastmod es la fecha de modificación del sitemap, no necesariamente la de publicación.
        ET.SubElement(item, 'pubDate').text = email.utils.format_datetime(dt)
        anteriores[link] = item
        agregados += 1

    def orden(item):
        dt = fecha(item.findtext('pubDate', ''))
        return dt or datetime.min.replace(tzinfo=timezone.utc)

    ordenados = sorted(anteriores.values(), key=orden, reverse=True)[:MAX_ITEMS]
    print(f'Artículos con título y fecha disponibles en sitemap: {len(nuevos)}')
    print(f'Nuevas añadidas: {agregados}')
    print(f'RSS total: {len(ordenados)}')
    print('Aviso: sitemap lastmod puede no ser la fecha original de publicación.')
    print('Aviso: artículos sin image:title quedan pendientes; no se inventan titulares.')
    if not agregados:
        print('No hay nuevas entradas verificables; se conserva el RSS anterior.')
        return

    rss = ET.Element('rss', {'version': '2.0'})
    channel = ET.SubElement(rss, 'channel')
    ET.SubElement(channel, 'title').text = 'EFE — Todas las noticias'
    ET.SubElement(channel, 'link').text = BASE
    ET.SubElement(channel, 'description').text = 'Noticias EFE obtenidas de metadatos públicos del sitemap'
    ET.SubElement(channel, 'language').text = 'es-ES'
    ET.SubElement(channel, 'lastBuildDate').text = email.utils.format_datetime(datetime.now(timezone.utc))
    for item in ordenados:
        channel.append(item)
    ET.indent(rss, space='  ')
    contenido = ET.tostring(rss, encoding='unicode', xml_declaration=True)
    escribir(RSS, contenido + '\n')
    # Conservar el estado anterior: evita alterar el seguimiento del scraper antiguo.
    print('estado.json se conserva sin cambios.')


if __name__ == '__main__':
    main()
