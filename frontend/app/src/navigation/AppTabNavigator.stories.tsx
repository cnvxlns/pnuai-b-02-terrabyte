import type { Meta, StoryObj } from '@storybook/react-native-web-vite';
import { fn } from 'storybook/test';

import { AppTabNavigator } from './AppTabNavigator';

const meta = {
  title: 'navigation/AppTabNavigator',
  component: AppTabNavigator,
  parameters: {
    layout: 'fullscreen',
  },
  args: {
    compact: false,
    cropName: '방울토마토',
    selectedCrop: 'cherry_tomato',
    onCreatePot: fn(),
    onLogout: fn(),
    onSelectCrop: fn(),
    onSelectPot: fn(),
    onUpdatePot: fn(),
    pots: [
      { id: 1, deviceId: 1, label: '화분 1', cropCode: 'cherry_tomato', status: 'ONLINE', autoControlEnabled: true },
      { id: 2, deviceId: 1, label: '화분 2', cropCode: 'basil', status: 'OFFLINE', autoControlEnabled: true },
    ],
    selectedPotId: 1,
  },
} satisfies Meta<typeof AppTabNavigator>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Desktop: Story = {};

export const Compact: Story = {
  args: {
    compact: true,
  },
};
