import type { Meta, StoryObj } from '@storybook/react-native-web-vite';
import { fn } from 'storybook/test';
import {
  storybookGuideScore as mockScore,
  storybookGuideMeasurements as mockMeasurements,
  storybookGuideSoilRecommendation as mockSoilRecommendation,
} from '../../data';

import type { EnvironmentScore, LatestMeasurements } from '../../measurement/measurementApi';
import { DeviceEnvironmentProvider } from '../../shared/device-environment/DeviceEnvironmentProvider';
import type { SoilRecommendation } from '../../soil/soilApi';
import { GuideScreen } from './GuideScreen';




const meta = {
  title: 'screens/guide/GuideScreen',
  component: GuideScreen,
  args: {
    compact: false,
    onNavigate: fn(),
    pot: {
      id: 1,
      deviceId: 1,
      label: '화분 1',
      cropCode: 'cherry_tomato',
      cropSelectedAt: '2026-07-14T02:00:00Z',
      status: 'ONLINE',
    },
  },
  render: (args) => (
    <DeviceEnvironmentProvider
      potId={1}
      fetchMeasurements={async () => mockMeasurements}
      fetchScore={async () => mockScore}
      fetchSoilRecommendation={async () => mockSoilRecommendation}
    >
      <GuideScreen {...args} />
    </DeviceEnvironmentProvider>
  ),
} satisfies Meta<typeof GuideScreen>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Default: Story = {};

export const WithPreChecks: Story = {
  render: (args) => (
    <DeviceEnvironmentProvider
      potId={1}
      fetchMeasurements={async () => mockMeasurements}
      fetchScore={async () => mockScore}
      fetchSoilRecommendation={async () => ({
        ...mockSoilRecommendation,
        preChecks: ['상토에 포함된 펄라이트 양 확인', '배수구 개방 상태 확인', '받침에 고인 물 확인'],
      })}
    >
      <GuideScreen {...args} />
    </DeviceEnvironmentProvider>
  ),
};
