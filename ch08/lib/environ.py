import ptan
import gym
import gym.spaces
from gym.utils import seeding
import enum
import numpy as np

from . import data

DEFAULT_BARS_COUNT = 10
DEFAULT_COMMISSION_PERC = 0.1


class Actions(enum.Enum):
    Skip = 0
    Buy = 1
    Close = 2


class State:
    def __init__(self, bars_count, comission_perc, reset_on_close, reward_on_close=True):
        assert isinstance(bars_count, int)
        assert bars_count > 0
        assert isinstance(comission_perc, float)
        assert comission_perc >= 0.0
        assert isinstance(reset_on_close, bool)
        assert isinstance(reward_on_close, bool)
        self.bars_count = bars_count
        self.comission = comission_perc / 100.0
        self.reset_on_close = reset_on_close
        self.reward_on_close = reward_on_close

    def reset(self, prices, offset):
        assert isinstance(prices, data.Prices)
        assert offset >= self.bars_count-1
        self.have_position = False
        self.open_price = 0.0
        self._prices = prices
        self._offset = offset

    def __len__(self):
        # [h, l, c] * bars + position_flag + rel_profit (since open)
        return 3*self.bars_count + 1 + 1

    def encode(self):
        """
        Convert current state into numpy array.
        """
        return self._encode(self.have_position, self.open_price)

    def _encode(self, have_position, open_price):
        """
        Utility function to easily tweak order
        """
        res = np.ndarray(shape=(len(self), ), dtype=np.float32)
        shift = 0
        for bar_idx in range(-self.bars_count+1, 1):
            res[shift] = self._prices.high[self._offset + bar_idx]
            shift += 1
            res[shift] = self._prices.low[self._offset + bar_idx]
            shift += 1
            res[shift] = self._prices.close[self._offset + bar_idx]
            shift += 1
        res[shift] = float(have_position)
        shift += 1
        if not have_position:
            res[shift] = 0.0
        else:
            res[shift] = (self._cur_close() - open_price) / open_price
        return res

    def _cur_close(self):
        """
        Calculate real close price for the current bar
        """
        open = self._prices.open[self._offset]
        rel_close = self._prices.close[self._offset]
        return open * (1.0 + rel_close)

    def step(self, action):
        """
        Perform one step in our price, adjust offset, check for the end of prices
        and handle position change
        :param action:
        :return: reward, done
        """
        assert isinstance(action, Actions)
        reward = 0.0
        done = False
        if action == Actions.Buy and not self.have_position:
            self.have_position = True
            close = self._cur_close()
            self.open_price = close
            reward -= close * self.comission
        elif action == Actions.Close and self.have_position:
            reward -= self._cur_close() * self.comission
            done |= self.reset_on_close
            if self.reward_on_close:
                reward += self._cur_close() - self.open_price
            self.have_position = False
            self.open_price = 0.0

        self._offset += 1
        done |= self._offset >= self._prices.close.shape[0]-1

        if self.have_position:
            # delta position profit equals cur bar change
            reward += self._prices.open[self._offset] * self._prices.close[self._offset]

        return reward, done


class StocksEnv(gym.Env):
    metadata = {'render.modes': ['human']}

    def __init__(self, prices, bars_count=DEFAULT_BARS_COUNT,
                 comission=DEFAULT_COMMISSION_PERC, reset_on_close=True):
        assert isinstance(prices, dict)
        self._prices = prices
        self._state = State(bars_count, comission, reset_on_close)
        self.action_space = gym.spaces.Discrete(n=len(Actions))
        self.observation_space = gym.spaces.Box(low=-np.inf, high=np.inf, shape=(len(self._state), ))
        self._seed()

    def _reset(self):
        # make selection of the instrument and it's offset. Then reset the state
        self._instrument = self.np_random.choice(list(self._prices.keys()))
        prices = self._prices[self._instrument]
        bars = self._state.bars_count
        offset = self.np_random.choice(prices.high.shape[0]-bars*10) + bars
        self._state.reset(prices, offset)
        return self._state.encode()

    def _step(self, action_idx):
        action = Actions(action_idx)
        reward, done = self._state.step(action)
        obs = self._state.encode()
        info = {"instrument": self._instrument, "offset": self._state._offset}
        return obs, reward, done, info

    def _render(self, mode='human', close=False):
        pass

    def _close(self):
        pass

    def _seed(self, seed=None):
        self.np_random, seed1 = seeding.np_random(seed)
        seed2 = seeding.hash_seed(seed1 + 1) % 2 ** 31
        return [seed1, seed2]

    @classmethod
    def from_dir(cls, data_dir, **kwargs):
        prices = {name: data.load_relative(file) for name, file in data.price_files(data_dir)}
        return StocksEnv(prices, **kwargs)

    def pretrain_data(self, gamma):
        result = []
        bars_count = self._state.bars_count
        for prices in self._prices.values():
            offsets = list(range(bars_count, prices.high.shape[0] - bars_count))
            result.extend(generate_pretrain_one_step_orders(self._state, prices, offsets, gamma))
            result.extend(generate_pretrain_no_orders(self._state, prices, offsets, gamma))
        return result


def generate_pretrain_one_step_orders(state, prices, offsets, gamma):
    """
    Generate transitions for one-step orders
    :yield: generated transitions
    """
    for ofs in offsets:
        state.reset(prices, ofs)
        o_state = state.encode()
        o_r, _ = state.step(Actions.Buy)
        c_r, done = state.step(Actions.Close)
        reward = o_r + gamma * c_r
        yield ptan.experience.ExperienceFirstLast(o_state, Actions.Buy.value, reward, None)


def generate_pretrain_no_orders(state, prices, offsets, gamma):
    """
    Generate transitions for one-step orders
    :yield: generated transitions
    """
    for ofs in offsets:
        state.reset(prices, ofs)
        o_state = state.encode()
        yield ptan.experience.ExperienceFirstLast(o_state, Actions.Skip.value, 0.0, None)
        yield ptan.experience.ExperienceFirstLast(o_state, Actions.Close.value, 0.0, None)
