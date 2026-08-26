import type { Meta, StoryObj } from '@storybook/react-native-web-vite';

import { Header } from './Header';
import { NotificationInboxProvider } from '../notification/NotificationInboxContext';

const meta = {
  title: 'navigation/Header',
  component: Header,
  parameters: {
    layout: 'fullscreen',
  },
  decorators: [
    (Story) => (
      <NotificationInboxProvider enabled={false}>
        <Story />
      </NotificationInboxProvider>
    ),
  ],
} satisfies Meta<typeof Header>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Dashboard: Story = {
  args: {
    compact: false,
    page: 'dashboard',
    onCreatePot: async () => undefined,
    onSelectPot: () => undefined,
    onUpdatePot: async () => undefined,
    pots: [{ id: 1, deviceId: 1, label: '화분 1', cropCode: 'cherry_tomato', status: 'ONLINE', autoControlEnabled: true }],
    selectedPotId: 1,
  },
};

export const Shop: Story = {
  args: {
    compact: false,
    page: 'shop',
    onCreatePot: async () => undefined,
    onSelectPot: () => undefined,
    onUpdatePot: async () => undefined,
    pots: [{ id: 1, deviceId: 1, label: '화분 1', cropCode: 'cherry_tomato', status: 'ONLINE', autoControlEnabled: true }],
    selectedPotId: 1,
  },
};
