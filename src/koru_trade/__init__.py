"""KORU 원화 기준 분할매수/분할익절 자동매매 시스템.

이 패키지는 미국 상장 3배 레버리지 ETF ``KORU``
(Direxion Daily MSCI South Korea Bull 3X Shares)를 대상으로
한국 투자자의 **원화(KRW) 환산 실현수익률**을 기준으로
분할 매수와 분할 익절을 수행하는 매매 엔진과 백테스터를 제공한다.

핵심 설계 원칙
--------------
1. 모든 수익률 판정은 USD 가격이 아니라 환율·수수료·환전스프레드를 반영한
   원화 실현금액 기준으로 계산한다 (:mod:`koru_trade.pnl`).
2. 매매 의사결정 로직 (:mod:`koru_trade.strategy`) 은 부작용이 없는 순수 함수이며
   동일 입력에 항상 동일 결정을 반환한다. 그래야 백테스트와 실거래가 같은 코드를 쓴다.
3. 실거래 경로는 기본이 DRY_RUN 이다. 명시적으로 해제하지 않으면 주문이 나가지 않는다.
"""

from koru_trade.version import __version__

__all__ = ["__version__"]
