import { useEffect, useRef, useState } from 'react';
import { Pressable, ScrollView, StyleSheet, Text, TextInput, useWindowDimensions, View } from 'react-native';

import { font } from '../appTheme/glass';
import { palette } from '../appTheme/palette';
import { scaleTypography } from '../appTheme/scaleTypography';
import { typeScale } from '../appTheme/typography';
import { ActionButton } from '../components/ActionButton';
import { BrandMark } from '../components/BrandMark';
import { Surface } from '../components/Surface';
import { getCrops, selectPotCrop, type CropResponse } from '../crop/cropApi';
import { getDevice, registerDevice, type DeviceResponse } from '../device/deviceApi';
import type { FlowStage } from '../navigation/types';
import { createCultivationSpace, getCultivationSpaces, type CultivationSpaceResponse } from '../space/spaceApi';

type AreaUnit = 'SQUARE_METERS' | 'PYEONG';

const spaceTypeOptions = [
  { label: '건물 옥상', value: '건물 옥상' },
  { label: '실내 유휴공간', value: '실내 유휴공간' },
  { label: '지하 공간', value: '지하 공간' },
  { label: '공실', value: '공실' },
  { label: '베란다·테라스', value: '베란다·테라스' },
  { label: '기타', value: '기타' },
] as const;

const areaUnitOptions: Array<{ label: string; value: AreaUnit }> = [
  { label: 'm²', value: 'SQUARE_METERS' },
  { label: '평', value: 'PYEONG' },
];

function SelectField<T extends string>({
  disabled = false,
  onChange,
  options,
  placeholder,
  style,
  value,
}: {
  disabled?: boolean;
  onChange: (value: T) => void;
  options: ReadonlyArray<{ label: string; value: T }>;
  placeholder: string;
  style?: any;
  value: T | '';
}) {
  const [open, setOpen] = useState(false);
  const selectedLabel = options.find((option) => option.value === value)?.label;

  return (
    <View style={[styles.selectContainer, style]}>
      <Pressable
        accessibilityRole="button"
        accessibilityState={{ disabled, expanded: open }}
        disabled={disabled}
        onPress={() => setOpen((current) => !current)}
        style={[styles.input, styles.selectTrigger, disabled && styles.disabledButton]}
      >
        <Text style={[styles.selectValue, !selectedLabel && styles.selectPlaceholder]}>
          {selectedLabel ?? placeholder}
        </Text>
        <Text style={styles.selectArrow}>{open ? '▴' : '▾'}</Text>
      </Pressable>
      {open ? (
        <View style={styles.selectMenu}>
          {options.map((option) => {
            const selected = option.value === value;
            return (
              <Pressable
                key={option.value}
                onPress={() => {
                  onChange(option.value);
                  setOpen(false);
                }}
                style={[styles.selectOption, selected && styles.selectOptionSelected]}
              >
                <Text style={[styles.selectOptionText, selected && styles.selectOptionTextSelected]}>
                  {option.label}
                </Text>
              </Pressable>
            );
          })}
        </View>
      ) : null}
    </View>
  );
}

function convertToSquareMeters(value: number, unit: AreaUnit) {
  const squareMeters = unit === 'PYEONG' ? value * 3.305785 : value;
  return Math.round(squareMeters * 100) / 100;
}

export function SetupFlow({
  deviceId,
  selectedPotId,
  onBack,
  onCropSelected,
  onDeviceRegistered,
  onNext,
  selectedCropCode,
  stage,
}: {
  deviceId?: number;
  selectedPotId?: number;
  onBack: () => void;
  onCropSelected: (cropCode: string) => void;
  onDeviceRegistered: (device: DeviceResponse) => void;
  onNext: () => void;
  selectedCropCode: string;
  stage: Exclude<FlowStage, 'auth' | 'app'>;
}) {
  const { width } = useWindowDimensions();
  const compact = width < 780;
  const [gatewayOnline, setGatewayOnline] = useState(false);
  const [potOnline, setPotOnline] = useState(false);
  const [serialCode, setSerialCode] = useState('');
  const [spaceName, setSpaceName] = useState('');
  const [spaceType, setSpaceType] = useState<(typeof spaceTypeOptions)[number]['value'] | ''>('');
  const [areaSquareMeters, setAreaSquareMeters] = useState('');
  const [areaUnit, setAreaUnit] = useState<AreaUnit>('SQUARE_METERS');
  const [cultivationSpaces, setCultivationSpaces] = useState<CultivationSpaceResponse[]>([]);
  const [selectedSpaceId, setSelectedSpaceId] = useState<number | null>(null);
  const [spacesLoading, setSpacesLoading] = useState(false);
  const [spacesError, setSpacesError] = useState<string | null>(null);
  const [savingSpace, setSavingSpace] = useState(false);
  const [deviceError, setDeviceError] = useState<string | null>(null);
  const [registeringDevice, setRegisteringDevice] = useState(false);
  const [availableCrops, setAvailableCrops] = useState<CropResponse[]>([]);
  const [cropQuery, setCropQuery] = useState('');
  const [cropError, setCropError] = useState<string | null>(null);
  const [cropLoading, setCropLoading] = useState(false);
  const [selectingCrop, setSelectingCrop] = useState(false);
  const codeInputRef = useRef<TextInput>(null);
  const step = stage === 'device' ? 1 : stage === 'crop' ? 2 : 3;
  const enteredArea = Number(areaSquareMeters);
  const parsedAreaSquareMeters = convertToSquareMeters(enteredArea, areaUnit);
  const hasNewSpaceDetails = spaceName.trim().length > 0
    && spaceType.trim().length > 0
    && Number.isFinite(enteredArea)
    && Number.isFinite(parsedAreaSquareMeters)
    && parsedAreaSquareMeters > 0;
  const canRegisterDevice = serialCode.length === 6 && (selectedSpaceId !== null || hasNewSpaceDetails);
  const spaceOptions = [
    { label: '새 공간 입력', value: 'new' },
    ...cultivationSpaces.map((space) => ({ label: `${space.name} · ${space.spaceType}`, value: String(space.id) })),
  ];

  const updateSerialCode = (value: string) => {
    setSerialCode(value.replace(/\D/g, '').slice(0, 6));
    setDeviceError(null);
  };

  const selectSpace = (value: string) => {
    const selectedSpace = cultivationSpaces.find((space) => String(space.id) === value);
    setDeviceError(null);
    if (!selectedSpace) {
      setSelectedSpaceId(null);
      return;
    }
    setSelectedSpaceId(selectedSpace.id);
    setSpaceName(selectedSpace.name);
    setSpaceType(selectedSpace.spaceType as (typeof spaceTypeOptions)[number]['value']);
    setAreaSquareMeters(String(selectedSpace.areaSquareMeters));
    setAreaUnit('SQUARE_METERS');
  };

  const saveSpace = async () => {
    const parsedArea = convertToSquareMeters(Number(areaSquareMeters), areaUnit);
    if (!spaceName.trim() || !spaceType.trim() || !Number.isFinite(parsedArea) || parsedArea <= 0) {
      setDeviceError('새 공간의 이름, 유형, 면적을 모두 입력해 주세요.');
      return;
    }
    setDeviceError(null);
    setSavingSpace(true);
    try {
      const createdSpace = await createCultivationSpace({
        name: spaceName.trim(),
        spaceType: spaceType.trim(),
        areaSquareMeters: parsedArea,
      });
      setCultivationSpaces((spaces) => [...spaces, createdSpace]);
      setSelectedSpaceId(createdSpace.id);
      setAreaUnit('SQUARE_METERS');
    } catch (requestError) {
      setDeviceError(requestError instanceof Error ? requestError.message : '공간을 저장하지 못했습니다.');
    } finally {
      setSavingSpace(false);
    }
  };

  const submitDevice = async () => {
    if (serialCode.length !== 6) {
      setDeviceError('기기 코드 숫자 6자리를 입력해 주세요.');
      codeInputRef.current?.focus();
      return;
    }
    const parsedArea = convertToSquareMeters(Number(areaSquareMeters), areaUnit);
    if (selectedSpaceId === null && !spaceName.trim()) {
      setDeviceError('공간 이름을 입력해 주세요.');
      return;
    }
    if (selectedSpaceId === null && !spaceType.trim()) {
      setDeviceError('공간 유형을 입력해 주세요.');
      return;
    }
    if (selectedSpaceId === null && (!Number.isFinite(parsedArea) || parsedArea <= 0)) {
      setDeviceError('공간 면적은 0보다 큰 숫자로 입력해 주세요.');
      return;
    }

    setDeviceError(null);
    setRegisteringDevice(true);
    try {
      const registered = await registerDevice(selectedSpaceId !== null
        ? { serialCode, spaceId: selectedSpaceId }
        : {
          serialCode,
          spaceName: spaceName.trim(),
          spaceType: spaceType.trim(),
          areaSquareMeters: parsedArea,
        });
      onDeviceRegistered(registered);
      onNext();
    } catch (requestError) {
      setDeviceError(requestError instanceof Error ? requestError.message : '기기를 등록하지 못했습니다.');
    } finally {
      setRegisteringDevice(false);
    }
  };

  useEffect(() => {
    if (stage !== 'device') return undefined;
    let active = true;
    setSpacesLoading(true);
    setSpacesError(null);
    void getCultivationSpaces()
      .then((spaces) => {
        if (active) setCultivationSpaces(spaces);
      })
      .catch((error) => {
        if (active) setSpacesError(error instanceof Error ? error.message : '공간 목록을 불러오지 못했습니다.');
      })
      .finally(() => {
        if (active) setSpacesLoading(false);
      });
    return () => {
      active = false;
    };
  }, [stage]);

  useEffect(() => {
    if (stage !== 'setup' || !deviceId) {
      setGatewayOnline(false);
      setPotOnline(false);
      return undefined;
    }
    let active = true;
    // Polled from the real device rather than assumed after a delay. The old
    // timer reported "기기 연결 완료" 1.8 seconds after this screen opened,
    // whether or not anything had ever been plugged in — which is precisely the
    // screen where a person is standing at the hardware trying to find out.
    const poll = () => {
      void getDevice(deviceId)
        .then((device) => {
          if (!active) return;
          setGatewayOnline(device.status === 'ONLINE');
          setPotOnline((device.pots ?? []).some((pot) => pot.status === 'ONLINE'));
        })
        .catch(() => {
          if (!active) return;
          setGatewayOnline(false);
          setPotOnline(false);
        });
    };
    poll();
    const timer = setInterval(poll, 5000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [stage, deviceId]);

  useEffect(() => {
    if (stage !== 'crop') return undefined;
    let active = true;
    setCropLoading(true);
    setCropError(null);
    const timer = setTimeout(() => {
      void getCrops(cropQuery)
        .then((nextCrops) => {
          if (active) setAvailableCrops(nextCrops);
        })
        .catch((error) => {
          if (active) setCropError(error instanceof Error ? error.message : '작물 목록을 불러오지 못했습니다.');
        })
        .finally(() => {
          if (active) setCropLoading(false);
        });
    }, 250);
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [cropQuery, stage]);

  const submitCrop = async () => {
    if (!deviceId || !selectedPotId) {
      setCropError('작물을 선택할 화분 정보를 찾을 수 없습니다.');
      return;
    }
    setCropError(null);
    setSelectingCrop(true);
    try {
      const selection = await selectPotCrop(selectedPotId, selectedCropCode);
      onCropSelected(selection.crop.code);
      onNext();
    } catch (error) {
      setCropError(error instanceof Error ? error.message : '작물을 선택하지 못했습니다.');
    } finally {
      setSelectingCrop(false);
    }
  };

  const copy = {
    device: { title: '진단할 공간을 등록하세요', description: '공간 기본 정보와 공간분석 세트의 등록 번호를 입력하세요.' },
    crop: { title: '검토할 작물을 선택하세요', description: '선택한 작물을 기준으로 공간 적합도와 필요한 개선 조건을 분석합니다.' },
    setup: { title: '분석 키트를 설치하세요', description: '공간분석 세트와 토양분석 세트를 연결하면 진단과 모니터링이 시작됩니다.' },
  }[stage];

  return (
    <ScrollView contentContainerStyle={styles.setupPage}>
      <View style={styles.setupTopbar}>
        <View style={styles.setupBrand}>
          <BrandMark compact />
          <Text style={styles.setupBrandName}>TerraByte</Text>
        </View>
        <View style={styles.setupProgress}>
          {[1, 2, 3].map((item) => (
            <View key={item} style={[styles.setupProgressBar, item <= step && styles.setupProgressBarActive]} />
          ))}
        </View>
        <Text style={styles.setupStepText}>{step} / 3</Text>
      </View>

      <View style={[styles.setupFrame, compact && styles.setupFrameCompact]}>
        <View style={styles.setupIntro}>
          <Text style={styles.setupTitle}>{copy.title}</Text>
          <Text style={styles.setupDescription}>{copy.description}</Text>
          <Pressable onPress={onBack} style={styles.setupBackButton}>
            <Text style={styles.setupBackText}>이전 단계</Text>
          </Pressable>
        </View>

        <Surface style={styles.setupPanel}>
          {stage === 'device' ? (
            <View style={styles.deviceSetupContent}>
              <View style={styles.setupFieldGrid}>
                {spacesLoading ? <Text style={styles.spaceHelpText}>등록된 공간을 불러오는 중입니다.</Text> : null}
                {!spacesLoading && cultivationSpaces.length > 0 ? (
                  <View style={styles.field}>
                    <Text style={styles.fieldLabel}>기존 공간</Text>
                    <SelectField
                      disabled={registeringDevice}
                      onChange={selectSpace}
                      options={spaceOptions}
                      placeholder="새 공간을 입력하세요"
                      value={selectedSpaceId === null ? 'new' : String(selectedSpaceId)}
                    />
                    <Text style={styles.spaceHelpText}>기존 공간을 선택하면 새 공간을 만들지 않고 기기를 연결합니다.</Text>
                  </View>
                ) : null}
                {spacesError ? <Text style={styles.spaceHelpText}>{spacesError}</Text> : null}
                <View style={styles.field}>
                  <Text style={styles.fieldLabel}>공간 이름</Text>
                  <TextInput
                    editable={!registeringDevice}
                    maxLength={100}
                    onChangeText={(value) => { setSelectedSpaceId(null); setSpaceName(value); setDeviceError(null); }}
                    placeholder="예: 부산 도심 옥상 A"
                    placeholderTextColor={palette.muted}
                    style={styles.input}
                    value={spaceName}
                  />
                </View>
                <View style={styles.field}>
                  <Text style={styles.fieldLabel}>공간 유형</Text>
                  <SelectField
                    disabled={registeringDevice}
                    onChange={(value) => { setSelectedSpaceId(null); setSpaceType(value); setDeviceError(null); }}
                    options={spaceTypeOptions}
                    placeholder="공간 유형을 선택하세요"
                    value={spaceType}
                  />
                </View>
                <View style={styles.field}>
                  <Text style={styles.fieldLabel}>공간 면적</Text>
                  <View style={styles.areaInputRow}>
                    <TextInput
                      editable={!registeringDevice}
                      inputMode="decimal"
                      keyboardType="decimal-pad"
                      onChangeText={(value) => {
                        setSelectedSpaceId(null);
                        setAreaSquareMeters(value.replace(/[^0-9.]/g, ''));
                        setDeviceError(null);
                      }}
                      placeholder="예: 42"
                      placeholderTextColor={palette.muted}
                      style={[styles.input, styles.areaValueInput]}
                      value={areaSquareMeters}
                    />
                    <SelectField
                      disabled={registeringDevice}
                      onChange={(value) => { setAreaUnit(value); setDeviceError(null); }}
                      options={areaUnitOptions}
                      placeholder="단위"
                      style={styles.areaUnitSelect}
                      value={areaUnit}
                    />
                  </View>
                  {areaUnit === 'PYEONG' && enteredArea > 0 ? (
                    <Text style={styles.areaConversionText}>
                      {enteredArea}평 = {parsedAreaSquareMeters}m²로 저장됩니다.
                    </Text>
                  ) : null}
                </View>
              </View>
              <ActionButton
                disabled={registeringDevice || savingSpace || selectedSpaceId !== null || !hasNewSpaceDetails}
                label={savingSpace ? '공간 저장 중...' : '새 공간 저장'}
                onPress={() => void saveSpace()}
              />
              <View style={styles.registrationGuide}>
                <Text style={styles.registrationGuideTitle}>공간분석 세트 등록 번호</Text>
                <Text style={styles.registrationGuideBody}>키트 하단 라벨에 표시된 숫자 여섯 자리를 입력하면 이 공간과 측정 데이터가 연결됩니다.</Text>
                <Text style={styles.registrationTestCode}>개발 테스트 코드: 123456</Text>
              </View>
              <Pressable
                accessibilityLabel="6자리 기기 코드 입력"
                accessibilityRole="button"
                onPress={() => codeInputRef.current?.focus()}
                style={styles.codeInputContainer}
              >
                <TextInput
                  autoFocus
                  caretHidden
                  editable={!registeringDevice}
                  inputMode="numeric"
                  keyboardType="number-pad"
                  maxLength={6}
                  onChangeText={updateSerialCode}
                  ref={codeInputRef}
                  style={styles.hiddenCodeInput}
                  value={serialCode}
                />
                <View pointerEvents="none" style={styles.codeInputRow}>
                  {Array.from({ length: 6 }, (_, index) => (
                    <View
                      key={index}
                      style={[
                        styles.codeCell,
                        index === serialCode.length && serialCode.length < 6 && styles.codeCellActive,
                      ]}
                    >
                      <Text style={styles.codeDigit}>{serialCode[index] ?? ''}</Text>
                    </View>
                  ))}
                </View>
              </Pressable>
              {deviceError ? <Text accessibilityRole="alert" style={styles.authError}>{deviceError}</Text> : null}
              <ActionButton
                disabled={registeringDevice || !canRegisterDevice}
                label={registeringDevice ? '등록 중…' : '공간 등록 완료'}
                onPress={() => void submitDevice()}
              />
            </View>
          ) : null}

          {stage === 'crop' ? (
            <View style={styles.cropSetupContent}>
              <TextInput
                editable={!selectingCrop}
                onChangeText={setCropQuery}
                placeholder="작물 이름 검색"
                placeholderTextColor={palette.muted}
                style={styles.input}
                value={cropQuery}
              />
              <View style={styles.cropChoiceGrid}>
                {availableCrops.map((crop) => {
                  const selected = selectedCropCode === crop.code;
                  return (
                    <Pressable
                      disabled={selectingCrop}
                      key={crop.code}
                      onPress={() => onCropSelected(crop.code)}
                      style={[styles.cropChoice, selected && styles.cropChoiceSelected]}
                    >
                      <View style={styles.cropChoiceCopy}>
                        <Text style={styles.cropChoiceName}>{crop.name}</Text>
                        <Text style={styles.cropChoiceDescription}>{crop.description}</Text>
                      </View>
                      <View style={[styles.cropRadio, selected && styles.cropRadioSelected]} />
                    </Pressable>
                  );
                })}
              </View>
              {cropLoading ? <Text style={styles.cropChoiceDescription}>작물 목록을 불러오는 중…</Text> : null}
              {!cropLoading && !cropError && availableCrops.length === 0 ? (
                <Text style={styles.cropChoiceDescription}>검색 결과가 없습니다.</Text>
              ) : null}
              {cropError ? <Text accessibilityRole="alert" style={styles.authError}>{cropError}</Text> : null}
              <ActionButton
                disabled={selectingCrop || cropLoading || !selectedCropCode}
                label={selectingCrop ? '선택 저장 중…' : `${availableCrops.find((crop) => crop.code === selectedCropCode)?.name ?? '작물'} 선택`}
                onPress={() => void submitCrop()}
              />
            </View>
          ) : null}

          {stage === 'setup' ? (
            <View style={styles.deviceSetupContent}>
              <View style={styles.installSteps}>
                {[
                  '공간분석 세트를 후보 공간 중앙의 그늘지지 않는 위치에 놓아 주세요.',
                  '토양분석 세트를 재배 베드에 설치하고 수분·온도 센서를 흙에 꽂아 주세요.',
                  '두 키트의 전원을 연결하고 센서 표시등이 켜지는지 확인하세요.',
                ].map((item, index) => (
                  <View key={item} style={styles.installStep}>
                    <Text style={styles.installStepNumber}>{String(index + 1).padStart(2, '0')}</Text>
                    <Text style={styles.installStepText}>{item}</Text>
                  </View>
                ))}
              </View>
              <View style={[styles.connectionPanel, gatewayOnline && styles.connectionPanelReady]}>
                <View style={[styles.connectionDot, gatewayOnline && styles.connectionDotReady]} />
                <View>
                  <Text style={styles.connectionTitle}>
                    {gatewayOnline ? '공간분석 세트 연결됨' : '공간분석 세트 신호를 기다리는 중'}
                  </Text>
                  <Text style={styles.connectionDescription}>
                    {gatewayOnline
                      ? '게이트웨이가 서버에 연결되어 있습니다.'
                      : '전원과 네트워크를 확인해 주세요. 연결에는 잠시 시간이 걸릴 수 있습니다.'}
                  </Text>
                </View>
              </View>
              {/* Two panels, because they fail separately: a gateway can be
                  online while the pot's node says nothing, and that is the case
                  someone standing at the bed needs to be able to see. */}
              <View style={[styles.connectionPanel, potOnline && styles.connectionPanelReady]}>
                <View style={[styles.connectionDot, potOnline && styles.connectionDotReady]} />
                <View>
                  <Text style={styles.connectionTitle}>
                    {potOnline ? '토양분석 세트 연결됨' : '토양분석 세트 신호를 기다리는 중'}
                  </Text>
                  <Text style={styles.connectionDescription}>
                    {potOnline
                      ? '첫 번째 환경 데이터를 받았습니다.'
                      : '센서를 흙에 꽂고 전원 표시등을 확인해 주세요.'}
                  </Text>
                </View>
              </View>
              <ActionButton label="공간 진단 시작하기" onPress={onNext} />
            </View>
          ) : null}
        </Surface>
      </View>
    </ScrollView>
  );
}

const styles = StyleSheet.create(scaleTypography({
  disabledButton: { opacity: 0.5 },
  field: { gap: 7 },
  fieldLabel: { ...typeScale.label, color: palette.secondary, fontFamily: font },
  input: { ...typeScale.body, backgroundColor: 'rgba(255,255,255,0.48)', borderColor: palette.lineStrong, borderRadius: 12, borderWidth: 1, color: palette.text, fontFamily: font, minHeight: 54, paddingHorizontal: 16 },
  authError: { ...typeScale.label, color: palette.red, fontFamily: font, fontWeight: '700' },
  selectContainer: { position: 'relative' },
  selectTrigger: { alignItems: 'center', flexDirection: 'row', justifyContent: 'space-between' },
  selectValue: { ...typeScale.body, color: palette.text, flex: 1, fontFamily: font },
  selectPlaceholder: { color: palette.muted },
  selectArrow: { color: palette.greenDark, fontFamily: font, fontSize: 14, marginLeft: 10 },
  selectMenu: { backgroundColor: '#f4f8f5', borderColor: palette.lineStrong, borderRadius: 12, borderWidth: 1, marginTop: 4, overflow: 'hidden' },
  selectOption: { borderBottomColor: palette.line, borderBottomWidth: 1, paddingHorizontal: 16, paddingVertical: 13 },
  selectOptionSelected: { backgroundColor: palette.greenSoft },
  selectOptionText: { ...typeScale.bodyStrong, color: palette.secondary, fontFamily: font },
  selectOptionTextSelected: { color: palette.greenDark, fontWeight: '700' },
  areaInputRow: { alignItems: 'flex-start', flexDirection: 'row', gap: 10 },
  areaValueInput: { flex: 1 },
  areaUnitSelect: { width: 110 },
  areaConversionText: { ...typeScale.caption, color: palette.greenDark, fontFamily: font },
  spaceHelpText: { ...typeScale.caption, color: palette.muted, fontFamily: font },
  setupPage: { alignItems: 'center', flexGrow: 1, paddingBottom: 48, paddingHorizontal: 32 },
  setupTopbar: { alignItems: 'center', flexDirection: 'row', maxWidth: 1080, paddingVertical: 24, width: '100%' },
  setupBrand: { alignItems: 'center', flex: 1, flexDirection: 'row', gap: 9 },
  setupBrandName: { ...typeScale.cardTitle, color: palette.text, fontFamily: font },
  setupProgress: { flexDirection: 'row', gap: 6, maxWidth: 260, width: '35%' },
  setupProgressBar: { backgroundColor: palette.line, borderRadius: 999, flex: 1, height: 4 },
  setupProgressBarActive: { backgroundColor: palette.green },
  setupStepText: { ...typeScale.label, color: palette.muted, flex: 1, fontFamily: font, textAlign: 'right' },
  setupFrame: { alignItems: 'center', flex: 1, flexDirection: 'row', gap: 70, justifyContent: 'center', maxWidth: 980, width: '100%' },
  setupFrameCompact: { flexDirection: 'column', gap: 28 },
  setupIntro: { flex: 1, gap: 13, maxWidth: 370 },
  setupTitle: { ...typeScale.pageTitle, color: palette.text, fontFamily: font },
  setupDescription: { ...typeScale.body, color: palette.secondary, fontFamily: font },
  setupBackButton: { alignSelf: 'flex-start', borderBottomColor: palette.lineStrong, borderBottomWidth: 1, marginTop: 12, paddingBottom: 3 },
  setupBackText: { ...typeScale.button, color: palette.secondary, fontFamily: font },
  setupPanel: { flex: 1.2, maxWidth: 540, padding: 28, width: '100%' },
  deviceSetupContent: { gap: 22 },
  setupFieldGrid: { gap: 13 },
  registrationGuide: { backgroundColor: palette.greenSoft, borderRadius: 9, gap: 7, padding: 18 },
  registrationGuideTitle: { ...typeScale.cardTitle, color: palette.greenDark, fontFamily: font },
  registrationGuideBody: { ...typeScale.body, color: palette.secondary, fontFamily: font },
  registrationTestCode: { ...typeScale.label, color: palette.greenDark, fontFamily: font, marginTop: 4 },
  codeInputRow: { flexDirection: 'row', gap: 8, justifyContent: 'center' },
  codeInputContainer: { position: 'relative' },
  hiddenCodeInput: { height: 1, opacity: 0, position: 'absolute', width: 1 },
  codeCell: { alignItems: 'center', backgroundColor: palette.panelMuted, borderColor: palette.line, borderRadius: 8, borderWidth: 1, flex: 1, height: 58, justifyContent: 'center', maxWidth: 56 },
  codeCellActive: { borderColor: palette.green, borderWidth: 2 },
  codeDigit: { ...typeScale.cardTitle, color: palette.text, fontFamily: font },
  cropSetupContent: { gap: 18 },
  cropChoiceGrid: { flexDirection: 'row', flexWrap: 'wrap', gap: 9 },
  cropChoice: { alignItems: 'center', backgroundColor: palette.panelMuted, borderColor: palette.line, borderRadius: 9, borderWidth: 1, flexBasis: '47%', flexDirection: 'row', flexGrow: 1, gap: 10, minWidth: 210, padding: 14 },
  cropChoiceSelected: { backgroundColor: palette.greenSoft, borderColor: '#b8d7c3' },
  cropChoiceCopy: { flex: 1, gap: 3 },
  cropChoiceName: { ...typeScale.cardTitle, color: palette.text, fontFamily: font },
  cropChoiceDescription: { ...typeScale.caption, color: palette.muted, fontFamily: font },
  cropRadio: { borderColor: palette.lineStrong, borderRadius: 999, borderWidth: 1, height: 13, width: 13 },
  cropRadioSelected: { backgroundColor: palette.green, borderColor: palette.green, borderWidth: 4 },
  installSteps: { gap: 10 },
  installStep: { alignItems: 'center', borderBottomColor: palette.line, borderBottomWidth: 1, flexDirection: 'row', gap: 14, paddingBottom: 12 },
  installStepNumber: { ...typeScale.label, color: palette.green, fontFamily: font },
  installStepText: { ...typeScale.body, color: palette.text, flex: 1, fontFamily: font },
  connectionPanel: { alignItems: 'center', backgroundColor: palette.amberSoft, borderRadius: 9, flexDirection: 'row', gap: 11, padding: 15 },
  connectionPanelReady: { backgroundColor: palette.greenSoft },
  connectionDot: { backgroundColor: palette.amber, borderRadius: 999, height: 9, width: 9 },
  connectionDotReady: { backgroundColor: palette.green },
  connectionTitle: { ...typeScale.label, color: palette.text, fontFamily: font },
  connectionDescription: { ...typeScale.caption, color: palette.secondary, fontFamily: font, marginTop: 4 },
}));
