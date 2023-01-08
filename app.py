#! /usr/bin/env python3
#
# Copyright 2019 Joshua Watt <JPEW.hacker@gmail.com>
#
# SPDX-License-Identifier: MIT

from aiohttp import web
import stt
import numpy
import yaml
import time
import logging
from aiolib import wave
from asgiref.sync import sync_to_async

config = {}
model = None
routes = web.RouteTableDef()

def load_config():
    with open('config.yaml', 'r') as f:
        return yaml.safe_load(f)

def create_model(config):
    d = config['stt']
    model = stt.Model(d['model'])
    model.setBeamWidth(int(d.get('beam_width', '512')))
    model.enableExternalScorer(d.get('scorer','models/coqui-v1.0.0-huge-vocab/huge-vocabulary.scorer'))
    if 'scorer_alpha' in d and 'scorer_beta' in d:
        model.setScorerAlphaBeta(float(d.get('scorer_alpha')),float(d.get('scorer_beta')))
    
    

    if 'lm' in d and 'trie' in d:
        model.enableDecoderWithLM(d['alphabet'], d['lm'], d['trie'],
                float(d.get('lm_weight', '1.5')),
                float(d.get('valid_word_count_weight', '2.25')))

    return model

class ASyncContext(object):
    def __init__(self, model):
        self._model = model
        self.exec_time = 0
        self.num_frames = 0
        self.latency = 0
        self._last_sample_time = 0

    def _update_exec_time(self, start_time):
        self.exec_time += time.perf_counter() - start_time

    async def createStream(self):
        self._last_sample_time = time.perf_counter()
        self._stream_ctx = await sync_to_async(self._model.createStream)()
        self._update_exec_time(self._last_sample_time)

    async def feedRawAudioContent(self, frames):
        self._last_sample_time = time.perf_counter()

        b = numpy.frombuffer(frames, numpy.int16)
        self.num_frames += len(b)

        await sync_to_async(self._stream_ctx.feedAudioContent)(b)

        self._update_exec_time(self._last_sample_time)

    async def finishStream(self):
        start_time = time.perf_counter()
        text = await sync_to_async(self._stream_ctx.finishStream)()
        self._update_exec_time(start_time)
        self.latency = time.perf_counter() - self._last_sample_time
        return text

async def handle_stt_wav(request, ctx):
    async with wave.Wave_read(request.content) as wav:
        (nchannels, sampwidth, framerate, _, _, _) = wav.getparams()
        while True:
            frames = await wav.readframes(512)
            if not frames:
                break
            await ctx.feedRawAudioContent(frames)

        sample_time = ctx.num_frames / framerate

    return sample_time

async def handle_stt_raw(request, ctx, framerate, sampwidth):
    while True:
        frames = await request.content.read(sampwidth * 512)
        if not frames:
            break
        await ctx.feedRawAudioContent(frames)

    return ctx.num_frames / framerate

@routes.post('/stt')
async def handle_stt(request):
    logging.info("Processing Stream...")
    start_time = time.perf_counter()
    ctx = ASyncContext(model)
    await ctx.createStream()

    fmt = request.query.get('format', 'wav')

    if fmt == 'wav':
        sample_time = await handle_stt_wav(request, ctx)
    elif fmt == '16K_PCM16':
        sample_time = await handle_stt_raw(request, ctx, 16000, 2)
    else:
        raise web.HTTPBadRequest()

    text = await ctx.finishStream()
    logging.info("Inference took %.03fs for %.03fs audio sample with %.03fs latency. Total time: %.03fs" %
            (ctx.exec_time, sample_time, ctx.latency, time.perf_counter() - start_time))
    return web.Response(text=text)

def main():
    global model
    global config

    logging.basicConfig(level=logging.INFO)

    logging.info('Loading model...')
    start_time = time.perf_counter()
    config = load_config()
    model = create_model(config)
    logging.info('Model was loaded in %0.3fs' % (time.perf_counter() - start_time))

    app = web.Application()
    app.add_routes(routes)

    server_config = config.get('server', {})
    web.run_app(app, host=server_config.get('host', '0.0.0.0'), port=server_config.get('port', 8080))

if __name__ == '__main__':
    main()

