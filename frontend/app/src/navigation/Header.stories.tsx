import type { Meta, StoryObj } from '@storybook/react-native-web-vite';

import {
  storybookGuideScore as mockScore,
  storybookGuideMeasurements as mockMeasurements,
} from '../data';
import { DeviceEnvironmentProvider } from '../shared/device-environment/DeviceEnvironmentProvider';
import { Header } from './Header';

const meta = {
  title: 'navigation/Header',
  component: Header,
  parameters: {
    layout: 'fullscreen',
  },
  args: {
    compact: false,
    page: 'dashboard',
    onCreatePot: async () => undefined,
    onSelectPot: () => undefined,
    onUpdatePot: async () => undefined,
    pots: [{ id: 1, deviceId: 1, label: '화분 1', cropCode: 'cherry_tomato', status: 'ONLINE' }],
    selectedPotId: 1,
  },
  // 알림은 적합도 점수에서 파생되므로 Header도 프로바이더 안에서만 의미가 있다.
  render: (args) => (
    <DeviceEnvironmentProvider
      potId={1}
      fetchMeasurements={async () => mockMeasurements}
      fetchScore={async () => mockScore}
    >
      <Header {...args} />
    </DeviceEnvironmentProvider>
  ),
} satisfies Meta<typeof Header>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Dashboard: Story = {};

export const Shop: Story = {
  args: { page: 'shop' },
};

/** 모든 지표가 적정 범위여서 알림이 없는 상태. */
export const NoAlerts: Story = {
  render: (args) => (
    <DeviceEnvironmentProvider
      potId={1}
      fetchMeasurements={async () => mockMeasurements}
      fetchScore={async () => ({
        ...mockScore,
        factors: mockScore.factors.map((factor) => ({ ...factor, status: 'OK' as const, gap: 0 })),
      })}
    >
      <Header {...args} />
    </DeviceEnvironmentProvider>
  ),
};
