from abc import ABC, abstractmethod
import asyncio
from sqlalchemy.orm import sessionmaker
from sqlalchemy.sql import and_
from database.db_manager import DBManager
from database.models import Trade, AccountInfo, Position, Balance
from datetime import datetime
from utils.logger import logger  # Import the logger


# The contract size for options is 100 shares
OPTIONS_CONTRACT_SIZE = 100

class BaseBroker(ABC):
    def __init__(self, api_key, secret_key, broker_name, engine, prevent_day_trading=False):
        self.api_key = api_key
        self.secret_key = secret_key
        self.broker_name = broker_name.lower()
        self.db_manager = DBManager(engine)
        self.Session = sessionmaker(bind=engine)
        self.account_id = None
        self.prevent_day_trading = prevent_day_trading
        logger.info('Initialized BaseBroker', extra={
                    'broker_name': self.broker_name})

    @abstractmethod
    def connect(self):
        pass

    @abstractmethod
    def _get_account_info(self):
        pass

    @abstractmethod
    def _place_order(self, symbol, quantity, order_type, price=None):
        pass

    @abstractmethod
    def _place_option_order(self, symbol, quantity, order_type, price=None):
        pass

    @abstractmethod
    def _get_order_status(self, order_id):
        pass

    @abstractmethod
    def _cancel_order(self, order_id):
        pass

    @abstractmethod
    def _get_options_chain(self, symbol, expiration_date):
        pass

    @abstractmethod
    def get_current_price(self, symbol):
        pass

    @abstractmethod
    def get_positions(self):
        pass

    def get_account_info(self):
        '''Get the account information'''
        logger.info('Getting account information')
        try:
            account_info = self._get_account_info()
            self.db_manager.add_account_info(AccountInfo(
                broker=self.broker_name, value=account_info['value']))
            logger.info('Account information retrieved',
                        extra={'account_info': account_info})
            return account_info
        except Exception as e:
            logger.error('Failed to get account information',
                         extra={'error': str(e)})
            return None

    def has_bought_today(self, symbol):
        '''Check if the symbol has been bought today'''
        logger.info('Checking if bought today', extra={'symbol': symbol})
        today = datetime.now().date()
        try:
            with self.Session() as session:
                trades = session.query(Trade).filter(
                    and_(
                        Trade.symbol == symbol,
                        Trade.broker == self.broker_name,
                        Trade.order_type == 'buy',
                        Trade.timestamp >= today
                    )
                ).all()
                logger.info('Checked for trades today', extra={
                            'symbol': symbol, 'trade_count': len(trades)})
                return len(trades) > 0
        except Exception as e:
            logger.error('Failed to check if bought today',
                         extra={'error': str(e)})
            return False

    def update_positions(self, session, trade):
        '''Update the positions based on the trade'''
        logger.info('Updating positions', extra={'trade': trade})
        if trade.quantity == 0:
            logger.error('Trade quantity is 0, doing nothing', extra={'trade': trade})
            return
        try:
            position = session.query(Position).filter_by(
                symbol=trade.symbol, broker=self.broker_name, strategy=trade.strategy).first()
            if trade.order_type == 'buy':
                if position:
                    position.cost_basis = (
                        getattr(position, 'cost_basis', 0) + (trade.executed_price * trade.quantity)
                    )
                    position.quantity += trade.quantity
                    position.latest_price = trade.executed_price
                    position.timestamp = datetime.now()
                else:
                    position = Position(
                        broker=self.broker_name,
                        strategy=trade.strategy,
                        symbol=trade.symbol,
                        quantity=trade.quantity,
                        latest_price=trade.executed_price,
                        cost_basis=trade.executed_price * trade.quantity,
                    )
                    session.add(position)
            elif trade.order_type == 'sell':
                if position:
                    if position.quantity == trade.quantity:
                        logger.info('Deleting sold position', extra={'position': position})
                        session.delete(position)
                    elif position.quantity > trade.quantity:
                        cost_per_share = position.cost_basis / position.quantity
                        position.cost_basis = (
                            getattr(position, 'cost_basis', 0)  - (trade.quantity * cost_per_share)
                        )
                        position.quantity -= trade.quantity
                        position.latest_price = trade.executed_price
                        session.add(position)
                    elif position.quantity < 0:
                        logger.warning('Sell quantity exceeds current position quantity', extra={
                                     'trade': trade})
                        session.delete(position)
                else:
                    logger.error('No position found for trade', extra={'trade': trade})
            session.commit()
            logger.info('Position updated', extra={'position': position})
        except Exception as e:
            logger.error('Failed to update positions', extra={'error': str(e)})

    async def place_option_order(self, symbol, quantity, order_type, strategy, price=None):
        '''Place an order for an option'''
        logger.info('Placing order', extra={
                    'symbol': symbol, 'quantity': quantity, 'order_type': order_type, 'strategy': strategy})

        if self.prevent_day_trading and order_type == 'sell':
            if self.has_bought_today(symbol):
                logger.error('Day trading is not allowed. Cannot sell positions opened today.', extra={
                             'symbol': symbol})
                return None

        try:
            if asyncio.iscoroutinefunction(self._place_order):
                response = await self._place_option_order(symbol, quantity, order_type, price)
            else:
                response = self._place_option_order(
                    symbol, quantity, order_type, price)
            logger.info('Order placed successfully',
                        extra={'response': response})
            if not price:
                # If price is not provided, use the filled price from the response
                price = response.get('filled_price', None)

            trade = Trade(
                symbol=symbol,
                quantity=quantity,
                price=price,
                executed_price=price,
                order_type=order_type,
                status='filled',
                timestamp=datetime.now(),
                broker=self.broker_name,
                strategy=strategy,
                profit_loss=0,
                success='yes'
            )
            if order_type == 'sell':
                profit_loss = self.db_manager.calculate_profit_loss(trade)
                logger.info('Profit/Loss calculated', extra={'profit_loss': profit_loss})
                trade.profit_loss = profit_loss

            with self.Session() as session:
                session.add(trade)
                session.commit()
                self.update_positions(session, trade)

                # Fetch the latest cash balance for the strategy
                latest_balance = session.query(Balance).filter_by(
                    broker=self.broker_name, strategy=strategy, type='cash').order_by(Balance.timestamp.desc()).first()
                if latest_balance:
                    # Calculate the order cost
                    order_cost = trade.executed_price * quantity * OPTIONS_CONTRACT_SIZE

                    # Subtract the order cost from the cash balance
                    if order_type == 'buy':
                        new_balance_amount = latest_balance.balance - order_cost
                    else:  # order_type == 'sell'
                        new_balance_amount = latest_balance.balance + order_cost

                    # Create a new balance record with the updated cash balance
                    new_balance = Balance(
                        broker=self.broker_name,
                        strategy=strategy,
                        type='cash',
                        balance=new_balance_amount,
                        timestamp=datetime.now()
                    )
                    session.add(new_balance)
                    session.commit()
                else:
                    logger.info('No balance records found for {strategy} in {self.broker_name}')

                # Update the P/L for the trade
                if order_type == 'sell':
                    session.add(trade)
                    session.commit()
            return response
        except Exception as e:
            logger.error('Failed to place order', extra={'error': str(e)})
            return None

    async def place_order(self, symbol, quantity, order_type, strategy, price=None):
        '''Place an order for a stock'''
        logger.info('Placing order', extra={
                    'symbol': symbol, 'quantity': quantity, 'order_type': order_type, 'strategy': strategy})

        if self.prevent_day_trading and order_type == 'sell':
            if self.has_bought_today(symbol):
                logger.error('Day trading is not allowed. Cannot sell positions opened today.', extra={
                             'symbol': symbol})
                return None

        try:
            if asyncio.iscoroutinefunction(self._place_order):
                response = await self._place_order(symbol, quantity, order_type, price)
            else:
                response = self._place_order(
                    symbol, quantity, order_type, price)
            logger.info('Order placed successfully',
                        extra={'response': response})

            trade = Trade(
                symbol=symbol,
                quantity=quantity,
                price=response.get('filled_price', price),
                executed_price=response.get('filled_price', price),
                order_type=order_type,
                status='filled',
                timestamp=datetime.now(),
                broker=self.broker_name,
                strategy=strategy,
                profit_loss=0,
                success='yes'
            )

            with self.Session() as session:
                session.add(trade)
                session.commit()
                self.update_positions(session, trade)

                # Fetch the latest cash balance for the strategy
                latest_balance = session.query(Balance).filter_by(
                    broker=self.broker_name, strategy=strategy, type='cash').order_by(Balance.timestamp.desc()).first()
                if latest_balance:
                    # Calculate the order cost
                    order_cost = trade.executed_price * quantity

                    # Subtract the order cost from the cash balance
                    if order_type == 'buy':
                        new_balance_amount = latest_balance.balance - order_cost
                    else:  # order_type == 'sell'
                        new_balance_amount = latest_balance.balance + order_cost

                    # Create a new balance record with the updated cash balance
                    new_balance = Balance(
                        broker=self.broker_name,
                        strategy=strategy,
                        type='cash',
                        balance=new_balance_amount,
                        timestamp=datetime.now()
                    )
                    session.add(new_balance)
                    session.commit()
                else:
                    logger.info('No balance records found for {strategy} in {self.broker_name}')

            return response
        except Exception as e:
            logger.error('Failed to place order', extra={'error': str(e)})
            return None

    def get_order_status(self, order_id):
        '''Get the status of an order'''
        logger.info('Retrieving order status', extra={'order_id': order_id})
        try:
            order_status = self._get_order_status(order_id)
            with self.Session() as session:
                trade = session.query(Trade).filter_by(id=order_id).first()
                if trade:
                    self.update_trade(session, trade.id, order_status)
            logger.info('Order status retrieved', extra={
                        'order_status': order_status})
            return order_status
        except Exception as e:
            logger.error('Failed to get order status', extra={'error': str(e)})
            return None

    def cancel_order(self, order_id):
        '''Cancel an order'''
        logger.info('Cancelling order', extra={'order_id': order_id})
        try:
            cancel_status = self._cancel_order(order_id)
            with self.Session() as session:
                trade = session.query(Trade).filter_by(id=order_id).first()
                if trade:
                    self.update_trade(session, trade.id, cancel_status)
            logger.info('Order cancelled successfully', extra={
                        'cancel_status': cancel_status})
            return cancel_status
        except Exception as e:
            logger.error('Failed to cancel order', extra={'error': str(e)})
            return None

    def position_exists(self, symbol):
        '''Check if a position exists for a symbol in the brokerage account'''
        positions = self.get_positions()
        return symbol in positions

    def get_options_chain(self, symbol, expiration_date):
        '''Get the options chain for a symbol'''
        logger.info('Retrieving options chain', extra={
                    'symbol': symbol, 'expiration_date': expiration_date})
        try:
            options_chain = self._get_options_chain(symbol, expiration_date)
            logger.info('Options chain retrieved', extra={
                        'options_chain': options_chain})
            return options_chain
        except Exception as e:
            logger.error('Failed to retrieve options chain',
                         extra={'error': str(e)})
            return None

    def update_trade(self, session, trade_id, order_info):
        '''Update the trade with the order information'''
        try:
            trade = session.query(Trade).filter_by(id=trade_id).first()
            if not trade:
                logger.error('Trade not found for update',
                             extra={'trade_id': trade_id})
                return

            executed_price = order_info.get('filled_price', trade.price)
            trade.executed_price = executed_price
            profit_loss = self.db_manager.calculate_profit_loss(trade)
            success = "success" if profit_loss > 0 else "failure"

            trade.executed_price = executed_price
            trade.success = success
            trade.profit_loss = profit_loss
            session.commit()
            logger.info('Trade updated', extra={'trade': trade})
        except Exception as e:
            logger.error('Failed to update trade', extra={'error': str(e)})
