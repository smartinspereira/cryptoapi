import asyncio
import ccxt.async_support as ccxt
import websockets_api.exchange as exchange
from ccxt.base.errors import BaseError
from websockets_api.errors import UnknownResponse


class Coinbasepro(exchange.Exchange, ccxt.coinbasepro):

    def __init__(self, params={}):
        super(ccxt.coinbasepro, self).__init__(params)
        self.channels = {
            'public': {
                'ticker': {
                    'ex_name': 'ticker',
                    'has': True
                },
                'trades': {
                    'ex_name': 'matches',
                    'has': True
                },
                'order_book': {
                    'ex_name': 'level2',
                    'has': True
                },
                'ohlcv': {
                    'ex_name': '',
                    'has': False
                }
            },
            'private': {}
        }
        flat_channels = {name: data
                         for _, v in self.channels.items()
                         for name, data in v.items()}
        self.channels_by_ex_name = {v['ex_name']: {'name': symbol,
                                                   'has': v['has']}
                                    for symbol, v in flat_channels.items()}
        self.max_channels = 1000000  # Maximum number of channels per connection. No limit for coinbasepro
        self.max_connections = {'public': (1, 4000), 'private': (0, 0)}
        self.connections = {'public': {}, 'private': {}}
        self.pending_channels = {'public': {}, 'private': {}}
        self.result = asyncio.Queue(maxsize=1)
        self.ws_endpoint = {'public': 'wss://ws-feed.pro.coinbase.com',
                            'private': ''}
        self.order_book = {}

    async def subscribe_ticker(self, symbols):
        ids = [self.markets[s]['id'] for s in symbols]
        name = self.channels['public']['ticker']['ex_name']
        requests = [{'type': 'subscribe',
                     'channels': [{'name': name, 'product_ids': [id]}]}
                    for id in ids]
        await self.subscription_handler(requests, public=True)

    async def subscribe_trades(self, symbols):
        ids = [self.markets[s]['id'] for s in symbols]
        requests = [{'type': 'subscribe',
                     'product_ids': [id],
                     'channels': [self.channels['public']['trades']['ex_name']]}
                    for id in ids]
        await self.subscription_handler(requests, public=True)

    async def subscribe_order_book(self, symbols):
        ids = [self.markets[s]['id'] for s in symbols]
        requests = [{'type': 'subscribe',
                     'product_ids': [id],
                     'channels': [self.channels['public']['order_book']['ex_name']]}
                    for id in ids]
        await self.subscription_handler(requests, public=True)

    async def build_unsubscribe_request(self, channel):
        return {'type': 'unsubscribe',
                'product_ids': channel['ex_channel_id'][0],
                'channels': [channel['ex_channel_id'][1]]}

    def parse_reply(self, reply, websocket, public):
        # Administrative replies
        if reply['type'] == 'subscriptions':
            return self.parse_subscribed(reply, websocket, public)
        elif reply['type'] == 'unsubscribe':
            return self.parse_unsubscribed(reply)
        elif reply['type'] == 'error':
            return self.parse_error(reply)
        # Market data replies
        id = reply['product_id']
        market = self.markets_by_id[id]
        if reply['type'] == 'ticker':
            return self.parse_ticker_(reply, market)
        elif reply['type'] in ['snapshot', 'l2update']:
            return self.parse_order_book_(reply, market['symbol'])
        elif reply['type'] in ['matches', 'last_match']:
            return self.parse_trades_(reply, market)
        else:
            raise UnknownResponse(reply)

    def parse_subscribed(self, reply, websocket, public):
        ex_name = reply['channels'][0]['name']
        subed_ids = reply['channels'][0]['product_ids']  # List of subscribed markets
        subed_symbols = [
            c['symbol']
            for _, v in self.connections.items()
            for ws, channels in v.items()
            for c in channels
        ]  # List of subscribed and registered markets
        id, symbol = [
            (id, self.markets_by_id[id]['symbol'])
            for id in subed_ids
            if self.markets_by_id[id]['symbol'] not in subed_symbols
        ][0]  # Find the only subed id that isn't registered yet
        name = self.channels_by_ex_name[ex_name]['name']
        request = {
            'type': 'subscribe',
            'product_ids': [id],
            'channels': [{'name': ex_name, 'product_ids': [id]}]
        }
        channel = {
            'request': request,
            'channel_id': self.claim_channel_id(),
            'name': name,
            'symbol': symbol,
            'ex_channel_id': (ex_name, id)
        }
        self.connection_metadata_handler(websocket, channel, public)

    def parse_unsubscribed(self, reply):
        for c in exchange.Exchange.get_channels(self.connections):
            if c['ex_channel_id'] == (reply['product_ids'], reply['channels']):
                channel = c
                del c  # Unregister the channel
                return {'unsubscribed': channel['channel_id']}

    def parse_error(self, reply):
        err = f"Error: {reply['message']}."
        reason = f"Reason: {reply['reason']}" if super().key_exists(reply, 'reason') else ''
        raise BaseError(err + "\n" + reason)

    def parse_ticker_(self, ticker, market):
        return 'ticker', self.parse_ticker(ticker, market)

    def parse_trades_(self, trades, market):
        return 'trades', self.parse_trade(trades, market=market)

    def parse_order_book_(self, order_book, symbol):
        if order_book['type'] == 'snapshot':
            order_book = self.parse_order_book(order_book)
            self.order_book[symbol] = {'bids': order_book['bids'], 'asks': order_book['asks']}
        else:
            for change in order_book['changes']:
                self_order_book = self.order_book[symbol]['bids'] if 'buy' in change else self.order_book[symbol]['asks']
                price = float(change[1])
                amount = float(change[2])
                existing_prices = [o[0] for o in self_order_book]
                if price in existing_prices:
                    idx = existing_prices.index(price)
                    if amount == 0:
                        del self_order_book[idx]
                    else:
                        self_order_book[idx] = [price, amount]
                else:
                    self_order_book.append([price, amount])
        timeframe = self.milliseconds()
        self.order_book[symbol].update({
            'timeframe': timeframe,
            'datetime': self.iso8601(timeframe),
            'nonce': None
        })
        self.order_book[symbol]['bids'] = sorted(self.order_book[symbol]['bids'], key=lambda l: l[0], reverse=True)
        self.order_book[symbol]['asks'] = sorted(self.order_book[symbol]['asks'], key=lambda l: l[0])
        return 'order_book', {symbol: self.order_book[symbol]}