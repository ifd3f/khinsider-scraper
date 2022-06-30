from multiprocessing.pool import ThreadPool
from aiohttp import ClientSession
from aiostream import stream

import asyncio
from concurrent.futures import Executor, ThreadPoolExecutor
import csv
from dataclasses import dataclass
import itertools
import logging
from multiprocessing.dummy import current_process
from pathlib import Path
from sqlite3 import Connection
import sys
from tempfile import TemporaryFile
import time
from traceback import print_exception
from typing import Coroutine, Iterable, List, NamedTuple, TextIO

import requests
from bs4 import BeautifulSoup
from .model import create_tables

from .parse import SongInfo, get_album_links_on_letter_page, get_last_letter_page, get_mp3_on_song_page, get_songs_on_album_page

logger = logging.getLogger(__name__)

max_attempts = 10


class ScrapeContext(NamedTuple):
    dburl: str
    pool: ThreadPool

    def get_db(self) -> Connection:
        return Connection(self.dburl)


def build_index(ctx: ScrapeContext) -> Iterable[SongInfo]:
    logger.info("Initializing")

    with ctx.get_db() as db:
        create_tables(db)
    enumerate_pages(ctx)
    enumerate_albums(ctx)


def enumerate_pages(ctx: ScrapeContext):
    with ctx.get_db() as db:
        if db.execute('SELECT COUNT(*) FROM albumpages').fetchone()[0] > 0:
            logger.info(f'Song page count already enumerated')
            return

    logger.info(f'Counting number of pages of songs')
    html = requests.get("https://downloads.khinsider.com/game-soundtracks").text

    soup1 = BeautifulSoup(html, features='html5lib')
    count = get_last_letter_page(soup1)
    logger.info(f'There are {count} pages of songs')
    links = get_album_links_on_letter_page(soup1)

    with ctx.get_db() as db:
        db.executemany(
            'INSERT INTO albums(album_url) VALUES (?)',
            ((l,) for l in links)
        )
        db.executemany(
            'INSERT INTO albumpages(page, visited) VALUES (?, ?)',
            [(1, 1)] + [(i, 0) for i in range(2, count + 1)]
        )


def enumerate_albums(ctx: ScrapeContext):
    def task(page: int):
        url = f"https://downloads.khinsider.com/game-soundtracks?page={page}"
        logger.info(f'Fetching album listing page {page} at URL {url}')
        html = requests.get(url).text

        soup = BeautifulSoup(html, features='html5lib')
        links = list(get_album_links_on_letter_page(soup))

        logger.info(f'Got {len(links)} albums on page {page}')

        with Connection(ctx.dburl) as conn:
            conn.execute('INSERT INTO albumpages(page, visited) VALUES (?, 1) ON CONFLICT DO UPDATE SET visited = 1', (page,))
            conn.executemany(
                'INSERT INTO albums(album_url) VALUES (?) ON CONFLICT DO NOTHING',
                ((l,) for l in links)        
            )

    logger.info('Crawling unvisited pages')
    with ctx.get_db() as db:
        rows = db.execute('SELECT page FROM albumpages WHERE visited = 0')
    ctx.pool.starmap(task, rows, chunksize=10)


async def fetch(self, cs: ClientSession, csvwriter: csv.writer, pool: Executor) -> Iterable['FetchTask']:
    logger.info(f'Fetching song at URL {self.song.url}')
    res = await cs.get(self.url)
    html = await res.read()

    def parse():
        soup = BeautifulSoup(html, features='html5lib')
        mp3 = get_mp3_on_song_page(soup, self.url)
        return mp3

    mp3 = await asyncio.get_event_loop().run_in_executor(pool, parse)

    csvwriter.writerow(self.song._replace(url=mp3))
    return []


async def fetch(self, cs: ClientSession, csvwriter: csv.writer, pool: Executor) -> Iterable['FetchTask']:
    logger.info(f'Fetching album at URL {self.url}')
    res = await cs.get(self.url)
    html = await res.read()

    def parse():
        soup = BeautifulSoup(html, features='html5lib')
        infos = list(get_songs_on_album_page(soup, self.url))
        return infos

    infos = await asyncio.get_event_loop().run_in_executor(pool, parse)
    logger.info(f'Found {len(infos)} songs at {self.url}')

    return (SongFetch(info) for info in infos)


async def fetch_and_store_song(song: SongInfo, cs: ClientSession) -> Iterable['FetchTask']:
    dest: Path = Path('songs') / song.file_path
    if dest.exists():
        logger.debug(f'{str(dest)} already exists')
        return

    logger.info('Fetching song %s', song)
    tempfile: Path = Path('.songcache') / song.file_path
    tempfile.parent.mkdir(parents=True, exist_ok=True)

    res = await cs.get(song.url)

    # First download to a temporary file so that incomplete files don't make their way into the results
    with tempfile.open("wb") as f:
        async for data in res.content.iter_chunked(1024):
            f.write(data)

    # Move the temporary file into the destination
    dest.parent.mkdir(parents=True, exist_ok=True)
    tempfile.rename(dest)
    logger.info(f'Downloaded {song.url} to {dest}')
    return []


async def download_all_song_infos(cs: ClientSession, db: Connection, n_workers=50) -> Iterable[SongInfo]:
    logger.info("Initializing")

    create_tables(db)

    task_queue: asyncio.Queue[FetchTask] = asyncio.Queue()
    for letter in letter_urls:
        task_queue.put_nowait(LetterFetch(letter))

    currently_processing: int = n_workers

    async def worker(pool: Executor):
        nonlocal currently_processing
        while True:
            currently_processing -= 1
            task = await task_queue.get()
            currently_processing += 1
            for i in range(max_attempts):
                logger.debug("Attempt %d/%d on %s", i + 1, max_attempts, task)
                try:
                    result = await task.fetch(cs, csvwriter, pool)
                except Exception:
                    logger.exception('Error while fetching object %s', task)
                    continue
                for i in result:
                    await task_queue.put(i)
                break

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        tasks = [
            asyncio.create_task(worker(pool))
            for _ in range(n_workers)
        ]

        # Wait until the queue is fully processed.
        while currently_processing > 0 or not task_queue.empty():
            await task_queue.join()

        # Cancel our worker tasks.
        for task in tasks:
            task.cancel()

    # Wait until all worker tasks are cancelled.
    await asyncio.gather(*tasks, return_exceptions=True)

