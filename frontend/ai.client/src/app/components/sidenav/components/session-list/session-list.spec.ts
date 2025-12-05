import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { SessionList } from './session-list';
import { SessionService } from '../../../../session/services/session/session.service';

describe('SessionList', () => {
  let component: SessionList;
  let fixture: ComponentFixture<SessionList>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [SessionList],
      providers: [
        provideRouter([]),
        SessionService
      ],
    })
    .compileComponents();

    fixture = TestBed.createComponent(SessionList);
    component = fixture.componentInstance;
    await fixture.whenStable();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
